from werkzeug.routing import Map, Rule


@service_activation_routes
def add_dynamodblocal_routes(routes: Map):
    routes.add(Rule("/shell", methods=["GET"], endpoint="dynamodb"))
    routes.add(Rule("/shell/<regex('.*'):req_path>", endpoint="dynamodb"))
