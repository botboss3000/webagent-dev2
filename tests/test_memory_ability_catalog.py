from app import abilities


def test_memory_is_a_configurable_knowledge_ability():
    abilities.reload()
    catalog = abilities.ui_catalog()

    knowledge = next(group for group in catalog["groups"] if group["name"] == "Knowledge")
    assert knowledge["id"] == "memory"
    assert knowledge["members"][:3] == ["context_control", "memory", "wiki_context"]

    memory = catalog["abilities"]["memory"]
    assert memory["display_name"] == "Memory"
    assert memory["tools"] == ["memory"]
    assert memory["simple"] is False

    settings = {
        field["key"]: field
        for field in abilities.ability_config_schema("memory")["settings"]
    }
    assert settings["memory_recall"]["default"] is True
    assert settings["memory_save"]["default"] is True


def test_memory_tool_is_not_duplicated_under_core_base():
    abilities.reload()
    catalog = abilities.ui_catalog()

    assert "memory" not in catalog["abilities"]["base"]["tools"]
    assert abilities.virtual_ability_for_tool("memory") == "memory"


def test_knowledge_group_sits_directly_after_core():
    abilities.reload()
    catalog = abilities.ui_catalog()

    group_ids = [group["id"] for group in catalog["groups"]]
    assert group_ids.index("memory") == group_ids.index("core") + 1
    assert group_ids[0] == "administrator"
