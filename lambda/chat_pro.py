"""
Central chat orchestration loop.

Intent:
- Keep chat.py untouched
- Keep this file readable and easy to debug
- Preserve downstream behavior:
  * stage sends
  * DB history load/save
  * timing labels
- Keep semantic logic out of this file:
  * no prompt text
  * no bag-of-words routing
  * no retrieval query construction
"""

import asyncio
import logging
from typing import Tuple

from app.core.chat.sources.web_source_agent import fetch_web_evidence
from app.core.chat.types import TurnContext, PersonaClassification
from app.core.chat.agents.turn_interpreter import interpret_turn
from app.core.chat.agents.persona_classifier import classify_persona
from app.core.chat.agents.planning_agent import plan_turn
from app.core.chat.agents.retrieval_planner import build_retrieval_plan
from app.core.chat.agents.answer_agent import stream_answer, generate_answer_text
from app.core.chat.agents.answer_agent import MODEL as ANSWER_MODEL
from app.core.evals.common import openai_cost_usd
from app.core.chat.agents.output_agent import render_output
from app.core.chat.sources.code_source_agent import fetch_code_evidence
from app.core.chat.evidence_bundle import build_evidence_bundle

from app.core.codebase.truncator import open_ai_truncator
from app.core.chat.telemetry import TurnTelemetry
from app.db.chat import get_session_conversation_history_from_db, get_custom_agent_by_id
from app.db.telemetry import persist_turn_telemetry
from app.utils.chat_utils import store_chat_in_db, summarize_early_exchanges
from app.constants import OPEN_AI_CLIENT
from app.core.errors import record_error

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Title helper
# ---------------------------------------------------------------------
async def generate_chat_title(title: str, question: str) -> str:
    if title == "New Chat":
        try:
            prompt = [
                {
                    "role": "system",
                    "content": "Generate a short chat title (2-3 words only) summarizing the user's question. No punctuation."
                },
                {
                    "role": "user",
                    "content": question
                }
            ]

            res = await OPEN_AI_CLIENT.chat.completions.create(
                model="gpt-4.1-nano",
                messages=prompt,
                max_completion_tokens=10
            )

            title = (res.choices[0].message.content or "").strip()
            final_title = title[:50] if title else question[:40]

        except Exception:
            log.warning("[chat_pro] title generation failed, using question prefix", exc_info=True)
            final_title = question[:40]

        return final_title

    return title


# ---------------------------------------------------------------------
# Stage helper
# ---------------------------------------------------------------------
async def send_stage(on_stream, stage: str):
    await on_stream(f"\n__STAGE__:{stage}\n")


# ---------------------------------------------------------------------
# History preparation
# ---------------------------------------------------------------------
async def _prepare_history_context(conversation_messages: list) -> Tuple[list, str]:
    if not conversation_messages:
        return [], ""

    recent_messages = conversation_messages[-4:]
    early_history_summary = ""

    if len(conversation_messages) > 4:
        try:
            early_history_summary = await summarize_early_exchanges(conversation_messages[:-4])
        except Exception:
            log.warning("[chat_pro] early history summarization failed, omitting summary", exc_info=True)
            early_history_summary = ""

    return recent_messages, early_history_summary


# ---------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------
async def chat(
    email_id: str,
    project_id: str,
    summary: str,
    user_question: str,
    uploaded_files: list,
    on_stream: callable,
    session_id: str,
    title: str,
    persona: str = None,
    agent: str = None,
    isDeepSearch: bool = False,
    isWebSearch: bool = False,
) -> str:
    raw_response_full = ""

    # Telemetry: in-memory only during the turn; persisted fire-and-forget at the end
    # so it never blocks streaming and a failure here can never break a chat.
    telem = TurnTelemetry()
    _telem_flushed = False

    def _flush_telemetry(answered: bool) -> None:
        nonlocal _telem_flushed
        if _telem_flushed:
            return
        _telem_flushed = True
        try:
            ctx = locals_ctx.get("context")
            persona_label = (
                ctx.persona_classification.label
                if ctx is not None and getattr(ctx, "persona_classification", None)
                else persona
            )
            telem.answered_substantively = answered
            row = telem.to_row(
                project_id=project_id,
                session_id=session_id,
                email_id=email_id,
                persona=persona_label,
                agent=agent,
                deep_search=isDeepSearch,
                web_search=isWebSearch,
            )
            # cost_usd priced off the answer-generation model/tokens only — this
            # pipeline's other sub-agent calls (turn interpreter, persona
            # classifier, planner, retrieval planner, code source agent) aren't
            # separately token-counted by TurnTelemetry today, so this is a
            # floor on true per-turn cost, not the full total. Chat's own
            # accounting path, separate from ingestion's cost_tracker.
            row["model"] = ANSWER_MODEL
            row["cost_usd"] = openai_cost_usd(ANSWER_MODEL, telem.tokens_prompt, telem.tokens_completion)
            asyncio.create_task(persist_turn_telemetry(row))
        except Exception:
            pass

    # `context` is assigned below; this dict lets the flush helper read it safely
    # whether or not we reached that point before an error.
    locals_ctx: dict = {}

    try:

        # -----------------------------------------------------------------
        # 1. Load DB context
        # -----------------------------------------------------------------
        telem.start_stage("understand")
        await send_stage(on_stream, "Understanding your request")
        conversation_messages, summary_for_planning, summary_for_answer = await asyncio.gather(
            get_session_conversation_history_from_db(email_id, project_id, session_id),
            open_ai_truncator(summary, "ignored", 50_000, project_id=project_id),
            open_ai_truncator(summary, "ignored", 120_000, project_id=project_id)
        )
        recent_messages, early_history_summary = await _prepare_history_context(conversation_messages)
        summary_for_intepreter = summary_for_planning

        # -----------------------------------------------------------------
        # 2. Build in-memory turn context
        # -----------------------------------------------------------------
        context = TurnContext(
            email_id=email_id,
            project_id=project_id,
            session_id=session_id,
            user_question=user_question,
            uploaded_files=uploaded_files or [],
            conversation_messages=conversation_messages or [],
            recent_messages=recent_messages or [],
            early_history_summary=early_history_summary or "",
            project_summary_for_planning=summary_for_planning or "",
            project_summary_for_answer=summary_for_answer or "",
            project_summary_for_intepreter=summary_for_intepreter or "",
            turn_interpretation=None,
            planning_decision=None,
            retrieval_plan=None,
            source_outputs={},
            evidence_bundle=None,
            answer_substance="",
            rendered_output="",
            trace=[],
            errors=[],
        )
        locals_ctx["context"] = context

        # -----------------------------------------------------------------
        # 3. Turn interpretation
        # -----------------------------------------------------------------
        context.turn_interpretation = await interpret_turn(context)

        # -----------------------------------------------------------------
        # 4. Persona classification
        # -----------------------------------------------------------------
        if agent and agent.strip():
            agent_label = agent.strip().lower()
            custom_steering = None

            if agent_label.startswith("custom_"):
                try:
                    custom_agent_id = int(agent_label.removeprefix("custom_"))
                    custom_agent = await get_custom_agent_by_id(email_id, custom_agent_id)
                    if custom_agent:
                        custom_steering = custom_agent["steering_prompt"]
                except (ValueError, TypeError):
                    pass

            context.persona_classification = PersonaClassification(
                label=agent_label,
                confidence=1.0,
                rationale="user-selected agent",
                custom_steering=custom_steering,
            )
        elif persona and persona.strip():
            context.persona_classification = PersonaClassification(
                label=persona.strip().lower(),
                confidence=1.0,
                rationale="user-selected via prompt enhancer",
            )
        else:
            context.persona_classification = await classify_persona(context)

        # -----------------------------------------------------------------
        # 5. Planning
        # -----------------------------------------------------------------
        context.planning_decision = await plan_turn(context)
        telem.end_stage("understand")

        # -----------------------------------------------------------------
        # 6. Retrieval gate
        # -----------------------------------------------------------------
        should_answer_now = context.planning_decision.proceed_to_answer

        code_required = "code" in context.planning_decision.required_sources
        code_helpful = "code" in context.planning_decision.helpful_sources

        needs_code_retrieval = (
            isDeepSearch or
            (should_answer_now
            and context.planning_decision.use_code_retrieval
            and context.planning_decision.evidence_need != "none"
            and (
                code_required
                or code_helpful
                or context.turn_interpretation.should_retrieve
            ))
        )

        if needs_code_retrieval or isWebSearch:
            telem.start_stage("retrieve")
            await send_stage(on_stream, "Finding relevant knowledge")

            # Prepare parallel tasks
            tasks = {}
            if needs_code_retrieval:
                context.retrieval_plan = await build_retrieval_plan(context)
                tasks["code"] = fetch_code_evidence(context)
            else:
                context.retrieval_plan = None
                context.source_outputs["code"] = None

            if isWebSearch:
                tasks["web"] = fetch_web_evidence(context)
            else:
                context.source_outputs["web"] = None

            # Execute all active retrieval tasks simultaneously
            if tasks:
                results = await asyncio.gather(*tasks.values(), return_exceptions=True)
                
                # Map results back to state
                for key, res in zip(tasks.keys(), results):
                    if isinstance(res, Exception):
                        log.warning("Error in %s retrieval: %s", key, res, exc_info=res)
                        context.source_outputs[key] = None
                    else:
                        context.source_outputs[key] = res

            # Capture retrieval-quality metrics for telemetry (lower distance = better)
            code_out = context.source_outputs.get("code")
            scs = list(getattr(code_out, "scores", []) or []) if code_out is not None else []
            telem.set_retrieval(
                docs_returned=getattr(code_out, "docs_returned", 0) if code_out is not None else 0,
                top_score=min(scs) if scs else None,
                mean_score=(sum(scs) / len(scs)) if scs else None,
                looked_thin=getattr(code_out, "retrieval_looks_thin", None) if code_out is not None else None,
                query_count=len(scs),
            )
            telem.end_stage("retrieve", docs=telem.retrieval.get("docs_returned", 0))

        else:
            context.retrieval_plan = None
            context.source_outputs["code"] = None
            context.source_outputs["web"] = None

        # -----------------------------------------------------------------
        # 7. Evidence bundle
        # -----------------------------------------------------------------
        telem.start_stage("bundle")
        await send_stage(on_stream, "Planning How to answer")
        context.evidence_bundle = build_evidence_bundle(context)
        telem.end_stage("bundle")
        # Prompt-token estimate: project summary + retrieved evidence + question
        try:
            code_out = context.source_outputs.get("code")
            rc = getattr(code_out, "retrieved_context", "") if code_out is not None else ""
            if isinstance(rc, list):
                rc = "\n".join(
                    (d.get("context", "") if isinstance(d, dict) else str(d)) for d in rc
                )
            telem.add_prompt_tokens(summary_for_answer or "")
            telem.add_prompt_tokens(rc or "")
            telem.add_prompt_tokens(user_question or "")
        except Exception:
            pass

        # -----------------------------------------------------------------
        # 8. Planner says do not answer substantively yet
        # -----------------------------------------------------------------
        if not should_answer_now:
            telem.start_stage("generate")
            await send_stage(on_stream, "Generating final response")
            raw_response_full = await stream_answer(context, on_stream)
            telem.mark_first_token()
            telem.end_stage("generate")
            telem.set_completion_tokens(raw_response_full or "")

            asyncio.create_task(
                store_chat_in_db(
                    email_id,
                    project_id,
                    user_question,
                    raw_response_full,
                    session_id,
                    await generate_chat_title(title, user_question),
                    agent=agent,
                )
            )

            _flush_telemetry(answered=False)
            return raw_response_full

        # -----------------------------------------------------------------
        # 9. Final answer generation
        # -----------------------------------------------------------------
        artifact_requested = context.turn_interpretation.artifact_requested

        telem.start_stage("generate")
        if artifact_requested == "default":
            await send_stage(on_stream, "Generating final response")
            raw_response_full = await stream_answer(context, on_stream)
        else:
            await send_stage(on_stream, "Drafting content...")
            context.answer_substance = await generate_answer_text(context)

            await send_stage(on_stream, "Generating final response")
            context.rendered_output = await render_output(
                context=context,
                answer_substance=context.answer_substance,
                on_stream=on_stream
            )
            raw_response_full = context.rendered_output
        telem.mark_first_token()
        telem.end_stage("generate")
        telem.set_completion_tokens(raw_response_full or "")

        # -----------------------------------------------------------------
        # 10. Persist final chat
        # -----------------------------------------------------------------
        asyncio.create_task(
            store_chat_in_db(
                email_id,
                project_id,
                user_question,
                raw_response_full,
                session_id,
                await generate_chat_title(title, user_question),
                agent=agent,
            )
        )

        _flush_telemetry(answered=True)
        return raw_response_full

    except asyncio.CancelledError:
        log.warning("[chat_pro] pipeline cancelled for project=%s, session=%s", project_id, session_id)
        telem.error = "cancelled"
        _flush_telemetry(answered=False)
        raise

    except Exception as e:
        log.warning(
            "[chat_pro] pipeline error for project=%s, session=%s: %s",
            project_id, session_id, e, exc_info=True,
        )
        await record_error(
            log, f"[chat_pro] pipeline error for project={project_id}, session={session_id}: {e}",
            project_id=project_id, exc=e, critical=True,
        )
        await on_stream(f"[ERROR] {str(e)}")
        telem.error = type(e).__name__
        _flush_telemetry(answered=False)
        return raw_response_full

