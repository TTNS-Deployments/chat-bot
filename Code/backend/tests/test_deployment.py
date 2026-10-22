"""Static checks that the Bicep source, the compiled ARM template, the SKU parameter files, the
portal form, the app settings and the README button agree."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

import validate_skus as v

ROOT = Path(__file__).resolve().parents[3]
ARM = ROOT / "Deployment" / "ARM"
TEMPLATE = json.loads((ARM / "azuredeploy.json").read_text())
PARAMS = TEMPLATE["parameters"]
UI = json.loads((ARM / "createUiDefinition.json").read_text())
PARAM_FILES = sorted((ARM / "parameters").glob("*.parameters.json"))
NOT_IN_UI = {"deployerPrincipalId", "deployerPrincipalType", "tags", "openAiSku", "shortResourceNames",
             "existingAppServicePlanName", "allowStorageSharedKey", "appPackageUrl", "enableDiagnostics"}


@pytest.mark.parametrize("path", PARAM_FILES, ids=lambda p: p.name)
def test_parameter_files_are_valid(path):
    values = v.load_values(path, [], PARAMS)
    assert v.validate_static(values, PARAMS) == []
    assert not set(values) & v.SCRIPT_MANAGED, "script-managed parameters must not be in SKU files"


def test_presets_exist():
    assert {p.name.split(".")[0] for p in PARAM_FILES} >= {"dev", "standard", "prod"}


def _ui_elements(elements):
    for e in elements:
        yield e
        yield from _ui_elements(e.get("elements", []))


def test_ui_outputs_are_parameters_and_cover_the_sku_choices():
    outputs = UI["parameters"]["outputs"]
    assert set(outputs) <= set(PARAMS)
    assert set(PARAMS) - NOT_IN_UI <= set(outputs)


def test_ui_dropdown_values_are_allowed_by_the_template():
    steps = [{"elements": UI["parameters"]["basics"]}, *UI["parameters"]["steps"]]
    for element in _ui_elements(e for s in steps for e in s["elements"]):
        name = {"partitionsBasic": "searchPartitionCount", "partitionsStandard": "searchPartitionCount"}.get(element["name"], element["name"])
        allowed = PARAMS.get(name, {}).get("allowedValues")
        if element["type"] == "Microsoft.Common.DropDown" and allowed:
            for option in element["constraints"]["allowedValues"]:
                assert option["value"] in allowed, f"{name}: {option['value']} not allowed by the template"


def test_app_settings_are_read_by_the_app():
    main_bicep = (ROOT / "Deployment" / "bicep" / "main.bicep").read_text()
    names = set(re.findall(r"^\s+([A-Z][A-Z_]+):", main_bicep, flags=re.M))
    platform = {"WEBSITE_RUN_FROM_PACKAGE", "SCM_DO_BUILD_DURING_DEPLOYMENT", "PYTHONPATH"}
    config = (ROOT / "Code" / "backend" / "chatbot" / "config.py").read_text()
    assert len(names) > 15, "no app settings found"
    for name in names - platform:
        assert f'"{name}"' in config, f"{name} is set by the template but never read"


@pytest.mark.skipif(not shutil.which("az"), reason="Azure CLI with Bicep not installed")
def test_azuredeploy_json_is_built_from_current_bicep(tmp_path):
    out = tmp_path / "azuredeploy.json"
    result = subprocess.run(["az", "bicep", "build", "--file", str(ROOT / "Deployment" / "bicep" / "main.bicep"),
                             "--outfile", str(out)], capture_output=True, text=True)
    if result.returncode != 0 and "bicep" in result.stderr.lower() and "install" in result.stderr.lower():
        pytest.skip("Bicep CLI not installed")
    assert result.returncode == 0, result.stderr

    def strip(node):
        if isinstance(node, dict):
            return {k: strip(val) for k, val in node.items() if k != "_generator"}
        if isinstance(node, list):
            return [strip(x) for x in node]
        return node

    assert strip(json.loads(out.read_text())) == strip(TEMPLATE), \
        "azuredeploy.json is stale: run Deployment/scripts/build_arm.sh"


def test_readme_deploy_button_points_at_this_repo():
    readme = (ROOT / "README.md").read_text()
    raw = "https://raw.githubusercontent.com/TTNS-Deployments/chat-bot/main/Deployment/ARM/"
    expected = ("https://portal.azure.com/#create/Microsoft.Template/uri/" + quote(raw + "azuredeploy.json", safe="")
                + "/createUIDefinitionUri/" + quote(raw + "createUiDefinition.json", safe=""))
    assert f"(https://aka.ms/deploytoazurebutton)]({expected})" in readme


# ---- validate_skus.py ----------------------------------------------------------------
def dev_values(**overrides):
    values = v.load_values(ARM / "parameters" / "dev.parameters.json", [], PARAMS)
    values.update(overrides)
    return values


@pytest.mark.parametrize("overrides, message", [
    ({"storageSku": "Premium_LRS"}, "storageSku='Premium_LRS' is not allowed"),
    ({"searchReplicaCount": 4}, "basic tier allows 1-3 replicas"),
    ({"searchSku": "standard", "searchReplicaCount": 12, "searchPartitionCount": 4}, "at most 36"),
    ({"chatCapacity": 0}, "below the minimum"),
    ({"logAnalyticsRetentionDays": 1000}, "above the maximum"),
    ({"solutionPrefix": "My-Bot"}, "lowercase"),
    ({"appServiceInstanceCount": "2"}, "expected int"),
    ({"unknownSku": "x"}, "not a parameter"),
    ({"chatModelName": " "}, "must not be empty"),
])
def test_static_validation_errors(overrides, message):
    errors = v.validate_static(dev_values(**overrides), PARAMS)
    assert any(message in e for e in errors), errors


def test_overrides_are_typed():
    values = v.load_values(ARM / "parameters" / "dev.parameters.json",
                           ["searchReplicaCount=2", "allowDocumentUpload=false", "searchSku=standard"], PARAMS)
    assert values["searchReplicaCount"] == 2 and values["allowDocumentUpload"] is False
    with pytest.raises(ValueError, match="whole number"):
        v.load_values(ARM / "parameters" / "dev.parameters.json", ["chatCapacity=lots"], PARAMS)
    with pytest.raises(ValueError, match="not a template parameter"):
        v.load_values(ARM / "parameters" / "dev.parameters.json", ["nope=1"], PARAMS)


def fake_az(models=None, usages=None, search_regions=("Central India",)):
    def run(args):
        if args[:2] == ["provider", "show"]:
            regions = list(search_regions) if args[3] == "Microsoft.Search" else ["Central India", "East US 2"]
            return {"resourceTypes": [{"resourceType": args[3].split(".")[1] and t, "locations": regions}
                                      for t in ("searchServices", "sites", "storageAccounts", "vaults", "accounts")]}
        if args[:3] == ["cognitiveservices", "model", "list"]:
            return models if models is not None else [
                {"model": {"name": "gpt-5-mini", "version": "2025-08-07", "skus": [{"name": "GlobalStandard"}]}},
                {"model": {"name": "text-embedding-3-small", "version": "1", "skus": [{"name": "GlobalStandard"}]}},
            ]
        if args[:3] == ["cognitiveservices", "usage", "list"]:
            return usages or []
        raise AssertionError(args)
    return run


def test_region_checks_pass_for_dev():
    errors, warnings = v.validate_region(dev_values(), PARAMS, az=fake_az())
    assert errors == [] and warnings == []


def test_region_checks_report_missing_model_service_and_quota():
    errors, warnings = v.validate_region(
        dev_values(chatDeploymentSku="Standard"), PARAMS,
        az=fake_az(search_regions=("West Europe",),
                   usages=[{"name": {"value": "OpenAI.GlobalStandard.text-embedding-3-small"}, "limit": 40, "currentValue": 20}]))
    assert any("Azure AI Search is not available in 'centralindia'" in e for e in errors)
    assert any("chat model gpt-5-mini version 2025-08-07 with deployment type Standard" in e for e in errors)
    assert any("embedding capacity 30K TPM" in w for w in warnings)


def test_cli_exit_codes(capsys):
    assert v.main(["-e", "dev", "-q"]) == 0
    assert v.main(["-e", "dev", "-q", "--set", "searchSku=premium"]) == 1
    assert "searchSku='premium' is not allowed" in capsys.readouterr().err
    assert v.main(["-e", "missing"]) == 2


def test_demo_preset_uses_the_cheapest_options():
    values = v.load_values(ARM / "parameters" / "demo.parameters.json", [], PARAMS)
    assert values["searchSku"] == "free" and values["semanticSearch"] == "disabled"
    assert values["existingAppServicePlanName"] and values["storageSku"] == "Standard_LRS"
    assert values["enableDiagnostics"] is False and values["authMode"] == "password"
    assert values["chatModelName"] == "gpt-5-mini" and values["openAiLocation"] == "southindia"
    assert values["embeddingModelName"] == "text-embedding-3-small" and values["embeddingLocation"] == "eastus"
    assert values["solutionPrefix"] == "chatbot" and values["shortResourceNames"] is True


@pytest.mark.parametrize("overrides, message", [
    ({"searchSku": "free", "searchReplicaCount": 2}, "no replicas or partitions"),
    ({"searchSku": "free", "semanticSearch": "free"}, "no semantic ranker"),
])
def test_free_tier_rules(overrides, message):
    errors = v.validate_static(dev_values(**overrides), PARAMS)
    assert any(message in e for e in errors), errors


def test_region_check_rejects_a_second_free_search_service():
    base = fake_az()

    def run(args):
        if args[:2] == ["resource", "list"]:
            return [{"name": "someone-else-search", "sku": {"name": "free"}}]
        return base(args)

    values = dev_values(searchSku="free", semanticSearch="disabled")
    errors, _ = v.validate_region(values, PARAMS, az=run)
    assert any("already has a Free search service" in e for e in errors)


def test_embedding_model_is_checked_in_its_own_region():
    base = fake_az()

    def run(args):
        if args[:3] == ["cognitiveservices", "model", "list"] and args[-1] == "eastus":
            return []
        return base(args)

    errors, _ = v.validate_region(dev_values(embeddingLocation="eastus"), PARAMS, az=run)
    assert any("embedding model" in e and "'eastus'" in e for e in errors)
    assert not any("chat model" in e for e in errors)
