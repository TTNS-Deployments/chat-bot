#!/usr/bin/env python3
"""Validate a SKU parameters file (plus overrides) before deploying.

    python3 Deployment/scripts/validate_skus.py -e dev
    python3 Deployment/scripts/validate_skus.py -e prod --set searchReplicaCount=3 --check-region

Static checks use the allowed values, ranges and lengths in Deployment/ARM/azuredeploy.json, so
the template stays the single source of truth, plus the Azure AI Search scale rules. With
--check-region the script also asks Azure (az CLI, signed in) whether the regions offer the
services, the models and the deployment types, and warns when Azure OpenAI quota looks too low.

Exit code 0 = valid, 1 = invalid, 2 = usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "Deployment" / "ARM" / "azuredeploy.json"
PARAMS_DIR = ROOT / "Deployment" / "ARM" / "parameters"

# Parameters the deploy scripts set themselves; they are not SKU choices.
SCRIPT_MANAGED = {"deployerPrincipalId", "deployerPrincipalType", "uploadApiKey"}

AzRunner = Callable[[list[str]], Any]


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------
def load_template(path: Path = TEMPLATE) -> dict:
    return json.loads(path.read_text())["parameters"]


def parameters_file(environment: str) -> Path:
    return PARAMS_DIR / f"{environment}.parameters.json"


def coerce(name: str, raw: str, spec: dict) -> Any:
    """Turn a --set value into the parameter's type."""
    kind = spec.get("type", "string").lower()
    if kind == "int":
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a whole number, got '{raw}'") from exc
    if kind == "bool":
        if raw.lower() in ("true", "1", "yes"):
            return True
        if raw.lower() in ("false", "0", "no"):
            return False
        raise ValueError(f"{name} must be true or false, got '{raw}'")
    if kind in ("object", "array"):
        return json.loads(raw)
    return raw


def load_values(path: Path, overrides: list[str], template: dict) -> dict:
    values = {k: v["value"] for k, v in json.loads(path.read_text())["parameters"].items()}
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"--set expects name=value, got '{item}'")
        name, raw = item.split("=", 1)
        if name not in template:
            raise ValueError(f"--set {name}: not a template parameter")
        values[name] = coerce(name, raw, template[name])
    return values


# --------------------------------------------------------------------------------------
# Static checks
# --------------------------------------------------------------------------------------
def _type_ok(value: Any, kind: str) -> bool:
    kind = kind.lower()
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "bool":
        return isinstance(value, bool)
    if kind in ("string", "securestring"):
        return isinstance(value, str)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    return True


def validate_static(values: dict, template: dict) -> list[str]:
    errors: list[str] = []
    for name, value in values.items():
        spec = template.get(name)
        if spec is None:
            errors.append(f"{name}: not a parameter of azuredeploy.json (typo?)")
            continue
        if not _type_ok(value, spec.get("type", "string")):
            errors.append(f"{name}: expected {spec['type']}, got {type(value).__name__} ({value!r})")
            continue
        allowed = spec.get("allowedValues")
        if allowed and value not in allowed:
            errors.append(f"{name}={value!r} is not allowed. Choose one of: {', '.join(map(str, allowed))}")
        if "minValue" in spec and value < spec["minValue"]:
            errors.append(f"{name}={value} is below the minimum {spec['minValue']}")
        if "maxValue" in spec and value > spec["maxValue"]:
            errors.append(f"{name}={value} is above the maximum {spec['maxValue']}")
        if isinstance(value, str):
            if "minLength" in spec and len(value) < spec["minLength"]:
                errors.append(f"{name}: at least {spec['minLength']} characters")
            if "maxLength" in spec and len(value) > spec["maxLength"]:
                errors.append(f"{name}: at most {spec['maxLength']} characters")

    def get(name: str) -> Any:
        if name in values:
            return values[name]
        return template.get(name, {}).get("defaultValue")

    prefix = get("solutionPrefix")
    if isinstance(prefix, str) and not re.fullmatch(r"[a-z0-9]{3,8}", prefix):
        errors.append("solutionPrefix: use 3-8 lowercase letters or digits (it becomes part of storage and Key Vault names)")

    sku, replicas, partitions = get("searchSku"), get("searchReplicaCount"), get("searchPartitionCount")
    if isinstance(replicas, int) and isinstance(partitions, int):
        if sku == "basic":
            if replicas > 3:
                errors.append(f"searchReplicaCount={replicas}: the basic tier allows 1-3 replicas")
            if partitions > 3:
                errors.append(f"searchPartitionCount={partitions}: the basic tier allows 1-3 partitions")
        elif replicas * partitions > 36:
            errors.append(
                f"searchReplicaCount x searchPartitionCount = {replicas * partitions}: "
                "Azure AI Search allows at most 36 search units"
            )

    for key in ("chatModelName", "chatModelVersion", "embeddingModelVersion"):
        if key in values and not str(values[key]).strip():
            errors.append(f"{key}: must not be empty")
    return errors


# --------------------------------------------------------------------------------------
# Region checks (need az login)
# --------------------------------------------------------------------------------------
def run_az(args: list[str]) -> Any:
    az = shutil.which("az")
    if not az:
        raise RuntimeError("Azure CLI (az) not found")
    result = subprocess.run([az, *args, "-o", "json"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"az {' '.join(args)} failed")
    return json.loads(result.stdout or "null")


def _norm(region: str) -> str:
    return region.replace(" ", "").lower()


def provider_regions(az: AzRunner, namespace: str, resource_type: str) -> set[str]:
    data = az(["provider", "show", "-n", namespace])
    for item in data.get("resourceTypes", []):
        if item.get("resourceType", "").lower() == resource_type.lower():
            return {_norm(loc) for loc in item.get("locations", [])}
    return set()


def model_offered(models: list[dict], name: str, version: str, deployment_sku: str) -> bool:
    for entry in models:
        model = entry.get("model", {})
        if model.get("name") == name and str(model.get("version")) == str(version):
            skus = {s.get("name") for s in model.get("skus", [])}
            return deployment_sku in skus
    return False


def validate_region(values: dict, template: dict, az: AzRunner = run_az, location: str | None = None) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    def get(name: str) -> Any:
        return values.get(name, template.get(name, {}).get("defaultValue"))

    region = location or get("location")
    if not isinstance(region, str) or region.startswith("["):
        warnings.append("location is not set in the parameters file; region checks use the resource group's region at deploy time")
        region = None
    aoai_region = get("openAiLocation")
    if not isinstance(aoai_region, str) or aoai_region.startswith("["):
        aoai_region = region

    checks = [
        ("Microsoft.Search", "searchServices", region, "Azure AI Search"),
        ("Microsoft.Web", "sites", region, "App Service"),
        ("Microsoft.Storage", "storageAccounts", region, "Storage"),
        ("Microsoft.KeyVault", "vaults", region, "Key Vault"),
        ("Microsoft.CognitiveServices", "accounts", aoai_region, "Azure OpenAI"),
    ]
    for namespace, rtype, where, label in checks:
        if not where:
            continue
        try:
            regions = provider_regions(az, namespace, rtype)
        except RuntimeError as exc:
            warnings.append(f"Could not check {label} regions: {exc}")
            continue
        if regions and _norm(where) not in regions:
            errors.append(f"{label} is not available in '{where}'")

    if aoai_region:
        try:
            models = az(["cognitiveservices", "model", "list", "-l", aoai_region]) or []
        except RuntimeError as exc:
            warnings.append(f"Could not list Azure OpenAI models in {aoai_region}: {exc}")
            models = None
        if models is not None:
            for prefix in ("chat", "embedding"):
                name, version, sku = get(f"{prefix}ModelName"), get(f"{prefix}ModelVersion"), get(f"{prefix}DeploymentSku")
                if not model_offered(models, name, version, sku):
                    errors.append(
                        f"{prefix} model {name} version {version} with deployment type {sku} is not offered in "
                        f"'{aoai_region}'. List what is: az cognitiveservices model list -l {aoai_region} -o table"
                    )
        try:
            usages = az(["cognitiveservices", "usage", "list", "-l", aoai_region]) or []
        except RuntimeError:
            usages = []
        by_name = {u.get("name", {}).get("value"): u for u in usages}
        for prefix in ("chat", "embedding"):
            key = f"OpenAI.{get(f'{prefix}DeploymentSku')}.{get(f'{prefix}ModelName')}"
            usage = by_name.get(key)
            if usage is None:
                continue
            free = float(usage.get("limit", 0)) - float(usage.get("currentValue", 0))
            want = get(f"{prefix}Capacity")
            if isinstance(want, int) and free < want:
                warnings.append(
                    f"{prefix} capacity {want}K TPM may exceed the free quota for {key} in {aoai_region} "
                    f"({free:g}K left). Lower {prefix}Capacity or request quota. (Ignore this when redeploying "
                    "over an existing deployment: its current capacity is counted as used.)"
                )
    return errors, warnings


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def summary(values: dict) -> str:
    rows = [(k, v) for k, v in values.items() if k not in SCRIPT_MANAGED and k != "tags"]
    width = max(len(k) for k, _ in rows) if rows else 0
    return "\n".join(f"  {k.ljust(width)}  {v}" for k, v in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-e", "--environment", help="preset name: Deployment/ARM/parameters/<name>.parameters.json")
    group.add_argument("-f", "--file", type=Path, help="path to a parameters file")
    parser.add_argument("--set", dest="overrides", action="append", default=[], metavar="NAME=VALUE",
                        help="override one parameter (repeatable)")
    parser.add_argument("-l", "--location", help="region used when the file has no location (resource group region)")
    parser.add_argument("--check-region", action="store_true", help="also check region, model and quota availability with az")
    parser.add_argument("-q", "--quiet", action="store_true", help="print errors only")
    args = parser.parse_args(argv)

    path = args.file or parameters_file(args.environment)
    if not path.is_file():
        available = ", ".join(sorted(p.name.split(".")[0] for p in PARAMS_DIR.glob("*.parameters.json")))
        print(f"Parameters file not found: {path}\nAvailable presets: {available}", file=sys.stderr)
        return 2

    template = load_template()
    try:
        values = load_values(path, args.overrides, template)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid input: {exc}", file=sys.stderr)
        return 2
    if args.location and "location" not in values:
        values["location"] = args.location

    errors = validate_static(values, template)
    warnings: list[str] = []
    if args.check_region and not errors:
        region_errors, warnings = validate_region(values, template, location=args.location)
        errors += region_errors

    if not args.quiet:
        print(f"SKU selection from {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}"
              + (f" with {len(args.overrides)} override(s)" if args.overrides else ""))
        print(summary(values))
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)
    if errors:
        print("\nThe SKU selection is not valid:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    if not args.quiet:
        print("SKU selection is valid" + (" for the chosen regions." if args.check_region else " (static checks; add --check-region to query Azure)."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
