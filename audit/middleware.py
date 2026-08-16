"""Request correlation.

Stamps each request with an id that audit events carry, so a support question
("what happened at 14:02?") can be answered by joining the audit trail to the
application logs instead of guessing from timestamps.

The id is generated here rather than taken from a client header: an
attacker-supplied correlation id can be used to poison somebody else's trail.
"""

import uuid


class RequestCorrelationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = uuid.uuid4().hex
        response = self.get_response(request)
        # Echoed so an operator reading a browser network tab can quote it.
        response.headers.setdefault('X-Request-ID', request.request_id)
        return response
