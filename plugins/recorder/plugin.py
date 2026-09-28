"""Standalone recorder discovery entry; no ability registry or agent tools.

application_plugins mounts router and owns lifecycle. GenUI templates stay in
this package so deleting plugins/recorder removes the feature without deleting
recordings or disturbing the application. See README.md and RECORDER-HOOK index.
"""
from pathlib import Path
from .api import router
from .service import get_service

async def start_background():
    await get_service().start()

async def stop_background():
    await get_service().shutdown()

async def genui_pages(user_id):
    from app.auth.identity import _is_admin
    if not await _is_admin(user_id):
        return []
    return [{"slug":"journey-reports","title":"Journey Reports",
             "description":"Recorded chat journeys, evidence and investigation plans",
             "order":40,"icon":"clapperboard","plugin":"recorder",
             "url":"/api/v1/genui/"+user_id+"/journey-reports/html",
             "agent_context":"Review recorder evidence. Execution requires report approval.",
             "session_config":{"target_name":"WebAgent","mode":"plan"}}]

async def genui_html(user_id,slug):
    if slug!="journey-reports" or not await genui_pages(user_id):
        return None
    folder=Path(__file__).with_name("genui")
    page=(folder/"index.html").read_text()
    css=(folder/"styles.css").read_text()
    js=(folder/"app.js").read_text()
    return page.replace("</head>","<style>"+css+"</style></head>").replace(
        "</body>","<script>"+js+"</script></body>")
