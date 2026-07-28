from app.constants import CLIENT
from app.db.chat import get_conversation_history_from_db, store_conversation_in_db

async def codebase_qa_with_anthropic(email_id: str, project_id: str, summary: str, user_question: str) -> str:
    try:
          # Get the relevant conversation history from the database
        conversation_messages = await get_conversation_history_from_db(email_id, project_id)
        
        # Prepare the messages for the API call
        api_messages = []
        
        # Add initial message with summary if conversation history is empty
        if not conversation_messages:
            
            api_messages.append({"role": "user", "content": user_question})
            await store_conversation_in_db(email_id, project_id, "user", user_question)
        else:
            # Ensure the conversation history alternates correctly
            for i, msg in enumerate(conversation_messages):
                if i % 2 == 0:
                    api_messages.append({"role": "user", "content": msg["content"]})
                else:
                    api_messages.append({"role": "assistant", "content": msg["content"]})
            
            # Add the new user question
            api_messages.append({"role": "user", "content": user_question})
            await store_conversation_in_db(email_id, project_id, "user", user_question)

        # Print api_messages for debugging
        print("API Messages:", api_messages)

        response = CLIENT.beta.prompt_caching.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2000,
            system=[
                    {"type": "text", "text": "You are an AI assistant tasked with analyzing codebases."},
                    {"type": "text", "text": f"Here is the summary of a complex codebase: {summary}", "cache_control": {"type": "ephemeral"}}
                ],
            messages=api_messages
        )

        # Add the AI's response to the conversation history
        ai_response = response.content[0].text
        await store_conversation_in_db(email_id, project_id, "assistant", ai_response)
        
        return ai_response
    except Exception as e:
        return str(e)