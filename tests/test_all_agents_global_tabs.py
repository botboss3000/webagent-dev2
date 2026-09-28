from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ability_delete, agents


ROOT = Path(__file__).resolve().parents[1]


def test_all_agents_card_exposes_global_tabs_in_requested_order():
    source = (ROOT / "ui/main-panel/agents/js/view.js").read_text(encoding="utf-8")

    assert "function _renderAllAgentsCard(container)" in source
    assert "const tabs = [['sessions', 'Sessions'], ['automations', 'Automations']]" in source
    assert "if (_userIsAdmin) tabs.push(['models', 'Models'])" in source
    assert "tabs.push(['abilities', 'Abilities'])" in source
    assert "_allAgentsModelTable = mountGlobalModelTable(body" in source
    assert "canEdit: _userIsAdmin" in source
    assert "setSessionsAgentContext(null, body)" in source


def test_all_agents_models_reuses_the_settings_global_mount():
    settings = (
        ROOT / "ui/main-panel/app/settings/agent-settings/agent-settings.js"
    ).read_text(encoding="utf-8")

    assert "export function mountGlobalModelTable(host, opts = {})" in settings
    assert "usageScope: 'global'" in settings
    assert "_modelTable = mountGlobalModelTable(host" in settings




def test_model_catalog_search_suppresses_password_manager_recall():
    source = (ROOT / "ui/shared/js/model-table.js").read_text(encoding="utf-8")

    assert "modelSearch.type = 'search'" in source
    assert "modelSearch.name = 'model-catalog-query'" in source
    assert "modelSearch.setAttribute('data-lpignore', 'true')" in source
    assert "modelSearch.setAttribute('data-1p-ignore', 'true')" in source
    assert "modelSearch.setAttribute('data-bwignore', 'true')" in source
    assert "modelSearch.setAttribute('data-form-type', 'other')" in source


def test_empty_shared_model_tables_expand_the_new_model_form():
    table = (ROOT / "ui/shared/js/model-table.js").read_text(encoding="utf-8")
    agent = (ROOT / "ui/main-panel/agents/configs/content.js").read_text(encoding="utf-8")
    admin = (
        ROOT / "ui/main-panel/app/settings/agent-settings/agent-settings.js"
    ).read_text(encoding="utf-8")

    empty_branch = table[table.index("if (!rows.length) {") : table.index("const cfgAnchor")]
    assert "table.classList.remove('ac-cfg-collapsed')" in empty_branch
    assert "mountModelTable(host" in agent
    assert "mountModelTable(host" in admin


def test_single_agent_model_table_uses_catalog_picker_without_losing_roles():
    # The per-agent Model page lives in configs/content.js; js/tab-config.js is a
    # thin re-export shim kept only for the lazy import in the Agent view.
    source = (ROOT / "ui/main-panel/agents/configs/content.js").read_text(encoding="utf-8")

    assert "userCatalog: true" in source
    assert "agentLocalRoster: true" in source
    assert "webagent_catalog: { name: 'WebAgent Catalog'" in source
    assert "platformCatalogModels.map(row" in source
    # MODEL-CATALOG-IDENTITY: catalog picker labels use the catalog name only; the
    # real "provider / model" string is never a picker label or a default alias.
    assert "name: row.display_name || 'WebAgent Catalog model'," in source
    assert "row.actual_model_name || row.model" not in source
    assert "credit_input_per_million: platform.credit_input_per_million" in source
    assert "credit_output_per_million: platform.credit_output_per_million" in source
    assert "saveDirectives: async (directives)" in source
    assert "subscriptionScope: 'agent'" in source
    table = (ROOT / "ui/shared/js/model-table.js").read_text(encoding="utf-8")
    assert "if (userCatalog) freeBtn.style.display = 'none';" in table


def test_usage_chart_legend_prefers_model_display_names():
    """MODEL-LEGEND-ALIAS: the usage chart labels a model series with the roster's
    display name, falling back to the raw model id only when no display name exists."""
    chart = (
        ROOT / "ui/main-panel/app/settings/agent-settings/model-analytics.js"
    ).read_text(encoding="utf-8")
    table = (ROOT / "ui/shared/js/model-table.js").read_text(encoding="utf-8")
    agent = (ROOT / "ui/main-panel/agents/configs/content.js").read_text(encoding="utf-8")
    view = (ROOT / "ui/main-panel/agents/js/view.js").read_text(encoding="utf-8")
    settings = (
        ROOT / "ui/main-panel/app/settings/agent-settings/agent-settings.js"
    ).read_text(encoding="utf-8")

    # The chart consults the roster's display name for model series first, keeps the
    # API's own label (sessions / agents) second, and the raw id last.
    assert "if (groupBy === 'models') {" in chart
    assert "const alias = modelLabels.get(series);" in chart
    assert "const label = labels && labels[series];" in chart
    assert "const raw = String(series);" in chart
    # A row with no display name must not register a label, so the raw id survives.
    assert "if (model && name && !next.has(model)) next.set(model, name);" in chart
    # The picker list and the legend share the resolver, so re-labelling must cover
    # the picker too when the roster arrives after the payload.
    assert "function renderModelPicker()" in chart
    assert "renderModelPicker();" in chart
    assert "setModelLabels," in chart

    # The shared table publishes its normalised roster after every load, and all
    # three chart+table surfaces subscribe with their own display-name map.
    assert "const onRoster = typeof opts.onRoster === 'function' ? opts.onRoster : null;" in table
    assert "if (onRoster) { try { onRoster(S.providers); } catch (_) {} }" in table
    assert "onRoster: (providers) => analytics?.setModelLabels(providers)," in agent
    assert (
        "onRoster: (providers) => _allAgentsModelAnalytics?.setModelLabels(providers),"
        in view
    )
    assert "onRoster: (providers) => _modelAnalytics?.setModelLabels(providers)," in settings


def test_opened_model_row_focuses_the_usage_chart():
    """MODEL-ROW-FOCUS: opening a saved row narrows the sibling usage chart to that
    model's series — replacing the per-row chart the table no longer renders — and
    closing the row hands the whole group back."""
    chart = (
        ROOT / "ui/main-panel/app/settings/agent-settings/model-analytics.js"
    ).read_text(encoding="utf-8")
    table = (ROOT / "ui/shared/js/model-table.js").read_text(encoding="utf-8")
    agent = (ROOT / "ui/main-panel/agents/configs/content.js").read_text(encoding="utf-8")
    view = (ROOT / "ui/main-panel/agents/js/view.js").read_text(encoding="utf-8")
    settings = (
        ROOT / "ui/main-panel/app/settings/agent-settings/agent-settings.js"
    ).read_text(encoding="utf-8")

    # The chart exposes the focus setter and drives the picker's own filter, so the
    # model checkboxes above the chart stay in step with the row that is open.
    assert "setModelFocus," in chart
    assert "visibleModels = key ? new Set([key]) : null;" in chart
    # Sessions/agents series are keyed by session/agent id, so a model focus cannot
    # apply there and is ignored while one of those groupings is selected.
    assert "if (model && groupBy !== 'models') return;" in chart

    # The shared table owns the focus: it fires the model id on open, '' on close,
    # and releases on every roster rebuild (which collapses the row details).
    assert (
        "const onModelFocus = typeof opts.onModelFocus === 'function' ? opts.onModelFocus : null;"
        in table
    )
    assert "if (open) _publishModelFocus(focusKey);" in table
    assert "else _releaseModelFocus();" in table
    assert "S.focusedModel = model;" in table
    assert "if (onModelFocus) { try { onModelFocus(''); } catch (_) {} }" in table

    # All three chart+table surfaces wire the focus through.
    assert "onModelFocus: (model) => analytics?.setModelFocus(model)," in agent
    assert "onModelFocus: (model) => _allAgentsModelAnalytics?.setModelFocus(model)," in view
    assert "onModelFocus: (model) => _modelAnalytics?.setModelFocus(model)," in settings


def test_expanded_model_row_drops_its_own_divider():
    """MODEL-ROW-GROUP: an open row's detail drops the body's top hairline and the
    divider moves down to the next row, so the detail reads as part of the row that
    opened it instead of as a separate box."""
    css = (ROOT / "ui/shared/css/app3.css").read_text(encoding="utf-8")

    assert ".ac-model-config > .ac-saved-injected.expanded > .ac-ability-body {" in css
    assert ".ac-model-config > .ac-saved-injected.expanded + .ac-saved-injected {" in css


def test_model_without_usage_keeps_the_chart_baseline_and_says_so():
    """MODEL-FOCUS-NOTE: opening a row whose model has no recorded usage must not
    narrow the chart to an empty plot. The baseline whole-group view is kept and a
    short note explains why, clearing after 2s or when clicked."""
    chart = (
        ROOT / "ui/main-panel/app/settings/agent-settings/model-analytics.js"
    ).read_text(encoding="utf-8")
    css = (ROOT / "ui/shared/css/app3.css").read_text(encoding="utf-8")

    # The no-usage branch holds the baseline instead of narrowing to a dead series.
    assert "if (visibleModels) { visibleModels = null; renderModelPicker(); render(); }" in chart
    assert "'Selected model has no usage yet'" in chart
    # Only reachable once the payload HAS loaded: while it is still null the chart has
    # not filled in yet, so the focus is remembered and applied when the data lands.
    assert "if (key && payload && !(Array.isArray(payload.models) ? payload.models : []).includes(key)) {" in chart
    # 2s auto-hide, plus click-to-dismiss.
    assert "focusNoteTimer = setTimeout(hideFocusNote, 2000);" in chart
    assert "note.addEventListener('click', hideFocusNote);" in chart
    # The note is card chrome, not part of the chart body that render() replaces.
    assert ".ac-model-analytics-note.on { opacity: 1; visibility: visible; }" in css


def test_reference_price_is_editable_for_byo_rows_only():
    """MODEL-PRICE-EDIT: a bring-your-own row's price is the OWNER's own estimate and
    is click-to-edit; a catalog row's price belongs to the app admin, is shown in
    house credits and is never editable."""
    table = (ROOT / "ui/shared/js/model-table.js").read_text(encoding="utf-8")
    settings = (
        ROOT / "ui/main-panel/app/settings/agent-settings/agent-settings.js"
    ).read_text(encoding="utf-8")
    rosters = (ROOT / "app/entitlements/rosters.py").read_text(encoding="utf-8")
    css = (ROOT / "ui/shared/css/app3.css").read_text(encoding="utf-8")

    # The roster schema must accept the override — an unknown field is rejected outright.
    assert '"reference_cost_input", "reference_cost_output",' in rosters
    assert '"reference_cost_input", "reference_cost_output"):' in rosters
    # Only the BYO branch hands out an editable value; the catalog branch names its owner.
    assert (
        "'<span class=\"ac-model-meta-v ac-model-meta-editable\" role=\"button\" tabindex=\"0\"'"
        in table
    )
    assert "'<span class=\"ac-model-price-note\">set by the app admin</span>'" in table
    assert "if (catalogRow) {" in table
    # Credits, not dollars, for the admin-owned catalog price.
    assert "function fmtCredits(inCredits, outCredits) {" in table
    # The edit commits the pair onto the row and saves through the roster.
    assert "p.reference_cost_input = nextIn;" in table
    assert "p.reference_cost_output = nextOut;" in table
    assert "delete p.reference_cost_input;" in table
    # The pair editor needs its own layout: the shared edit input is single-field wide.
    assert ".ac-model-meta-edit-group .ac-model-meta-edit-input { min-width: 0; width: 84px; }" in css
    # The admin settings roster whitelist carries the field through to the API.
    assert "{ reference_cost_input: Number(provider.reference_cost_input) }" in settings


def test_model_dropdown_escapes_vertically_constrained_agent_cards():
    source = (ROOT / "ui/shared/js/model-table.js").read_text(encoding="utf-8")

    assert "document.body.appendChild(modelDd)" in source
    assert "modelDd.style.position = 'fixed'" in source
    assert "const openAbove = spaceBelow < 160 && spaceAbove > spaceBelow" in source
    assert "const renderedHeight = Math.min(modelDd.scrollHeight, dropdownMaxHeight)" in source
    assert "document.addEventListener('scroll', scheduleModelDropdownPosition, true)" in source
    assert "window.visualViewport?.addEventListener('resize', scheduleModelDropdownPosition)" in source


def test_global_ability_table_has_a_read_only_mode():
    table = (ROOT / "ui/shared/js/admin-ability-table.js").read_text(encoding="utf-8")
    credentials = (ROOT / "ui/shared/js/dom-utils.js").read_text(encoding="utf-8")

    assert "const canEdit = cfg.canEdit !== false" in table
    assert "canEdit && !lockedOn" in table
    assert "if (canEdit) row.appendChild(_buildAbilityMoreButton" in table
    assert "opts.canEdit !== false && data.can_edit !== false" in credentials


def test_admin_settings_mutations_have_a_server_side_guard():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")

    assert "async def _admin_settings_mutation_guard" in source
    assert 'request.url.path.startswith("/admin/")' in source
    assert "await get_db().is_user_admin(caller_id)" in source


class _NonAdminDB:
    async def is_user_admin(self, _user_id):
        return False


def test_ability_repository_mutations_reject_non_admin(monkeypatch):
    monkeypatch.setattr("app.auth.identity.decode_token", lambda _token: {"user_id": "regular-user"})
    monkeypatch.setattr("app.db.get_db", lambda: _NonAdminDB())
    app = FastAPI()
    app.include_router(ability_delete.router)

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/abilities/example/skill",
            headers={"Authorization": "Bearer regular-user"},
            json={"content": "cannot write"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "App admin access required."


def test_global_ability_config_values_are_public_read_only(monkeypatch):
    class _Config:
        @staticmethod
        async def ensure_bootstrapped(_db):
            return None

        @staticmethod
        def get_ability_config(ability_id):
            assert ability_id == "example"
            return {"mode": "safe"}

    monkeypatch.setattr("app.admin.ability_config.ensure_bootstrapped", _Config.ensure_bootstrapped)
    monkeypatch.setattr("app.admin.ability_config.get_ability_config", _Config.get_ability_config)
    monkeypatch.setattr(agents, "get_db", lambda: object())
    app = FastAPI()
    app.include_router(agents.router)

    with TestClient(app) as client:
        response = client.get("/api/v1/abilities/example/config")

    assert response.status_code == 200
    assert response.json() == {"ability_settings": {"mode": "safe"}}


def test_usage_panel_is_pinned_and_summarises_the_range():
    """TIP-PINNED-PANEL: the info panel is pinned to the plot's top-left corner instead
    of chasing the pointer. It shows the RANGE summary by default — every visible series
    totalled across the visible days, plus a grand total — and swaps to a single day's
    breakdown while the pointer focuses one. Parking it in a corner is what keeps it
    clear of the bars and the dashed trend for good."""
    chart = (
        ROOT / "ui/main-panel/app/settings/agent-settings/model-analytics.js"
    ).read_text(encoding="utf-8")
    css = (ROOT / "ui/shared/css/app3.css").read_text(encoding="utf-8")
    settings = (
        ROOT / "ui/main-panel/app/settings/agent-settings/agent-settings.js"
    ).read_text(encoding="utf-8")
    view = (ROOT / "ui/main-panel/agents/js/view.js").read_text(encoding="utf-8")
    content = (
        ROOT / "ui/main-panel/agents/configs/content.js"
    ).read_text(encoding="utf-8")

    # The panel is pinned by CSS to the plot's top-left and never moves.
    assert "TIP-PINNED-PANEL" in chart
    assert ".ac-model-analytics-tip { position: absolute; z-index: 3; top: 8px; left: 8px;" in css
    # It must not swallow the chart's own pointer: hovering THROUGH the panel still works.
    assert "pointer-events: none; }" in css.split(".ac-model-analytics-tip {")[1][:400]

    # A range summary builder exists and totals each series across the visible days.
    assert "function summaryHtml(series, days, key, formatter, layout) {" in chart
    assert "value: days.reduce((sum, day) => sum + (Number(day?.models?.[item]?.[key]) || 0), 0)," in chart
    assert "const grand = totals.reduce((sum, entry) => sum + entry.value, 0);" in chart

    # The panel starts on the summary and returns to it whenever the focus is lost.
    assert "function showSummary() {" in chart
    assert "showSummary();" in chart
    # Losing the pointer must NOT hide the panel — it only reverts the content.
    assert "tip.setAttribute('hidden'" not in chart
    assert "tip.hidden" not in chart

    # The day readout swaps the CONTENT only; placement is never computed at pointer time.
    assert "tip.innerHTML = tooltipHtml(geometry.days[bucket], geometry.series, geometry.key, geometry.formatter);" in chart
    assert "tip.style.left" not in chart
    assert "tip.style.top" not in chart
    # The old pointer-chasing envelope geometry is gone entirely.
    assert "TIP-CLEAR-OF-CURVE" not in chart
    assert "curveSpanAt" not in chart
    assert "tip.offsetWidth" not in chart

    # The node and its charge still ride the curve — that part is untouched.
    assert "const picked = geometry.trendLines?.length ? curveTopAt(geometry.trendLines, vbX) : null;" in chart

    # Every surface that mounts the chart refetches the bumped module.
    assert "model-analytics.js?v=7" in settings
    assert "model-analytics.js?v=7" in view
    assert "model-analytics.js?v=7" in content
