exports.handler = async function(event) {
  var method = event.httpMethod;
  var path = event.path;
  if (path === "/") {
    return {
      isBase64Encoded: false,
      statusCode: 200,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: "Hello Shahan"
       })
    };
  }
  if (path === '/getEvent') {
    return {
      isBase64Encoded: false,
      statusCode: 200,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        eventMethod: method,
        eventPath: path,
        eventHeaders: event.headers | "Cant get headers",
        eventResource: event.resource | "Cant get resource"
      })
    }
  }
};