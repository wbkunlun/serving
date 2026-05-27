#!/usr/bin/env python3
"""Generate Knative Serving release YAMLs from config files.

Replaces ko:// image references with actual Docker Hub images and updates version labels.
"""

import os
import re
import sys
import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
OUTPUT_DIR = REPO_ROOT / "release-yamls"
VERSION = "v1.22.3"
REGISTRY = "docker.io/brycehuang/knative"
NET_CONTOUR_CONFIG = os.environ.get("NET_CONTOUR_CONFIG", "/tmp/net-contour/config")

# Image mapping: ko:// paths -> actual Docker Hub images
IMAGE_MAP = {
    # Main serving components
    "ko://knative.dev/serving/cmd/activator": f"{REGISTRY}:activator-{VERSION}",
    "ko://knative.dev/serving/cmd/autoscaler": f"{REGISTRY}:autoscaler-{VERSION}",
    "ko://knative.dev/serving/cmd/autoscaler-hpa": f"{REGISTRY}:autoscaler-hpa-{VERSION}",
    "ko://knative.dev/serving/cmd/controller": f"{REGISTRY}:controller-{VERSION}",
    "ko://knative.dev/serving/cmd/default-domain": f"{REGISTRY}:default-domain-{VERSION}",
    "ko://knative.dev/serving/cmd/queue": f"{REGISTRY}:queue-{VERSION}",
    "ko://knative.dev/serving/cmd/webhook": f"{REGISTRY}:webhook-{VERSION}",
    # net-contour
    "ko://knative.dev/net-contour/cmd/controller": f"{REGISTRY}:net-contour-controller-{VERSION}",
    # Post-install binaries use official knative releases on GCR (not built locally)
    "ko://knative.dev/serving/pkg/cleanup/cmd/cleanup": f"gcr.io/knative-releases/knative.dev/serving/pkg/cleanup/cmd/cleanup:v1.22.0",
    "ko://knative.dev/pkg/apiextensions/storageversion/cmd/migrate": f"gcr.io/knative-releases/knative.dev/pkg/apiextensions/storageversion/cmd/migrate:v1.22.0",
}

KO_PATTERN = re.compile(r"ko://[a-zA-Z0-9./\-_]+")


def replace_ko_refs(text):
    """Replace ko:// references in text."""
    def replacer(match):
        path = match.group(0)
        return IMAGE_MAP.get(path, path)
    return KO_PATTERN.sub(replacer, text)


def process_raw_yaml(content):
    """Process YAML content: replace ko:// refs and version labels."""
    content = replace_ko_refs(content)
    # Replace version labels
    content = re.sub(
        r'app\.kubernetes\.io/version: devel',
        f'app.kubernetes.io/version: "{VERSION}"',
        content
    )
    content = re.sub(
        r'^(\s*)version: devel',
        rf'\1version: "{VERSION.lstrip("v")}"',
        content,
        flags=re.MULTILINE
    )
    return content


def collect_yaml_files(config_dir):
    """Collect all .yaml files recursively, following symlinks but excluding vendor/."""
    files = []
    config_path = REPO_ROOT / config_dir
    for root, dirs, filenames in os.walk(config_path, followlinks=True):
        # Don't descend into vendor directories
        dirs[:] = [d for d in dirs if d != "vendor" and d != "node_modules"]
        dirs.sort()
        for fname in sorted(filenames):
            if fname.endswith(".yaml") and fname != "placeholder.go":
                fpath = Path(root) / fname
                # Skip files inside vendor directories
                if "/vendor/" in str(fpath) or fpath.is_symlink():
                    continue
                files.append(fpath)
    return files


def build_yaml_stream(files):
    """Build a concatenated YAML stream from multiple files."""
    docs = []
    for fpath in files:
        try:
            raw = fpath.read_text()
            processed = process_raw_yaml(raw)
            for doc in yaml.safe_load_all(processed):
                if doc is not None:
                    docs.append(doc)
        except Exception as e:
            print(f"  Warning: failed to process {fpath}: {e}", file=sys.stderr)
    return docs


def write_stream(files, output_file):
    """Write YAML documents to a file."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / output_file
    docs = []
    with open(out_path, "w") as f:
        for fpath in files:
            try:
                raw = fpath.read_text()
            except Exception:
                continue
            processed = process_raw_yaml(raw)
            for doc in yaml.safe_load_all(processed):
                if doc is not None:
                    yaml.dump(
                        doc, f,
                        allow_unicode=True,
                        default_flow_style=False,
                        sort_keys=False,
                        width=1000
                    )
                    f.write("---\n")
                    docs.append(doc)
    size_kb = out_path.stat().st_size / 1024
    return len(docs), size_kb


def main():
    print(f"Generating Knative Serving {VERSION} release YAMLs")
    print(f"Registry: {REGISTRY}")
    print(f"Output:   {OUTPUT_DIR}")
    print()

    # serving-core.yaml
    print("Building serving-core.yaml...")
    files = collect_yaml_files("config/core")
    count, size = write_stream(files, "serving-core.yaml")
    print(f"  -> {count} documents, {size:.1f} KB")

    # serving-crds.yaml
    print("Building serving-crds.yaml...")
    files = collect_yaml_files("config/core/300-resources")
    # Add knative caching CRD (not in vendor symlink path)
    caching_crd = REPO_ROOT / "vendor/knative.dev/caching/config/image.yaml"
    if caching_crd.exists():
        files.append(caching_crd)
    count, size = write_stream(files, "serving-crds.yaml")
    print(f"  -> {count} documents, {size:.1f} KB")

    # serving-net-contour.yaml
    print("Building serving-net-contour.yaml...")
    files = collect_yaml_files(NET_CONTOUR_CONFIG)
    count, size = write_stream(files, "serving-net-contour.yaml")
    print(f"  -> {count} documents, {size:.1f} KB")

    print()
    print("Done!")
    for f in sorted(OUTPUT_DIR.glob("*.yaml")):
        print(f"  {f.name}")


if __name__ == "__main__":
    main()
