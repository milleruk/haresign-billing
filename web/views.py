"""The Haresign Billing shell: landing page, liveness and readiness."""

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache


@never_cache
def home(request):
    return render(request, 'web/home.html')


@never_cache
def health(request):
    """Liveness only, and deliberately shallow.

    It does not touch PostgreSQL or Redis. A health check that queries the
    database conflates "this process is alive" with "its dependencies are
    healthy", and an orchestrator that believes them the same thing will kill and
    restart every healthy container during a database blip — turning a recoverable
    outage into a full one.
    """
    response = JsonResponse({'status': 'ok', 'service': 'haresign-billing'})
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


@never_cache
def ready(request):
    """Readiness: durable state and fail-closed throttle state are both usable.

    Both are required. The entitlement API and the webhook endpoint both fail
    closed when the cache is gone, so an instance that cannot reach Redis would
    refuse every provider delivery — which is a correct refusal and an incorrect
    thing to route traffic to.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        key = 'readiness:probe'
        cache.set(key, 'ok', timeout=10)
        if cache.get(key) != 'ok':
            raise RuntimeError('cache readiness probe failed')
        cache.delete(key)
    except Exception:
        response = JsonResponse({'status': 'unavailable'}, status=503)
    else:
        response = JsonResponse({'status': 'ready', 'service': 'haresign-billing'})
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response
