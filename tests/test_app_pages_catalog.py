import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/config/app-pages.json"
PAGE_ROOT = ROOT / "ui/main-panel/app/settings/pages"


def test_catalog_pages_are_first_class_settings_page_descriptors():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    ids = {page_id for group in catalog["groups"] for page_id in group["pageIds"]}
    assert "settings" not in ids
    assert catalog["headerPageIds"] == ["settings"]
    for page_id in ids:
        assert (PAGE_ROOT / page_id / "page.json").is_file()


def test_settings_uses_one_star_favorite_row_component():
    source = (ROOT / "ui/main-panel/app/settings/settings.js").read_text(encoding="utf-8")
    assert "settings-item-favorite" in source
    assert "settings-item-more" not in source
    assert "arrow-up-from-line" not in source
    assert "data-app-page-toggle" in source


def test_header_actions_remain_vertical_ellipsis_and_settings_is_protected():
    source = (ROOT / "ui/main-panel/app/instances.js").read_text(encoding="utf-8")
    assert 'data-lucide="ellipsis-vertical"' in source
    assert "pageId === 'settings' ? ''" in source
    assert "data-app-page-open" in source
