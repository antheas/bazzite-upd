import json
import logging
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Sequence
import os
from .model import MetaPackage
from .utils import tqdm

logger = logging.getLogger(__name__)


def get_ostree_map(repo: str, ref: str):
    # Prefix has a fixed length
    # unless filesize is larger than what fits
    prefix = len("d00555 0 0")
    hash_len = 64

    proc = None
    pbar = tqdm(desc=f"Reading OSTree ref '{ref}'", unit="files", total=300_000)

    cmd = [
        "ostree",
        "ls",
        "-C",
        "-R",
        "--repo",
        repo,
        ref,
    ]
    if os.getuid() != 0:
        cmd = ["sudo", *cmd]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
        )
        assert proc.stdout is not None

        mapping = {}
        hashes = {}
        while line := proc.stdout.readline().decode("utf-8"):
            # Skip directories and soft links
            if line[0] == "d":
                continue

            ofs = prefix - 1
            # Link count
            while line[ofs] != " ":
                ofs += 1
            # Space after
            while line[ofs] == " ":
                ofs += 1
            # Size
            size_start = ofs
            while line[ofs] != " ":
                ofs += 1
            size = int(line[size_start:ofs])

            fhash = line[ofs + 1 : ofs + 65]
            assert " " not in fhash, f"Hash fail: {fhash} | {line}"
            if line[0] == "l":
                end_i = line.rindex("->")
            else:
                end_i = -1
            fn = line[ofs + hash_len + 1 : end_i].strip()
            mapping[fn] = fhash
            hashes[fhash] = size
            pbar.update(1)
    finally:
        pbar.close()
        if proc is not None:
            proc.wait()

    if proc is not None:
        assert proc.poll() == 0, f"OSTree exited with error: {proc.returncode}"

    return mapping, hashes


def dump_ostree_packages(
    dedi_layers: list[list[MetaPackage]],
    layers: list[list[MetaPackage]],
    out_fn: str,
    mapping: dict[str, str],
    labels: dict[str, str],
    timestamp: str,
):
    # Create layer meta
    smeta = {}
    pkg_to_layer = {}

    def get_pkg_name(pkg: MetaPackage):
        return f"meta:{pkg.name}" if pkg.meta else pkg.name

    for layer in dedi_layers:
        layer_name = f"dedi:{get_pkg_name(layer[0])}"
        layer_human = layer_name

        unpackaged = len(layer) == 1 and "unpackaged" in layer[0].name
        if unpackaged:
            layer_name = "unpackaged"
            layer_human = "dedi:meta:unpackaged"

        smeta[layer_name] = layer_name
        for pkg in layer:
            pkg_to_layer[pkg.name] = layer_name

    for i, layer in enumerate(layers):
        layer_name = f"rechunk_layer{i:03d}"
        layer_human = ",".join([get_pkg_name(pkg) for pkg in layer])

        unpackaged = len(layer) == 1 and "unpackaged" in layer[0].name
        if unpackaged:
            layer_name = "unpackaged"
        smeta[layer_name] = layer_human

        for pkg in layer:
            pkg_to_layer[pkg.name] = layer_name

    if "unpackaged" not in smeta:
        logger.info("No unpackaged layer found. Creating it manually.")
        smeta["unpackaged"] = "dedi:meta:unpackaged"

    # Create mappings for hash -> layer
    ostree_out = {}
    used_layers = set()
    for ohash, pkg in mapping.items():
        layer = pkg_to_layer[pkg]
        ostree_out[ohash] = layer
        used_layers.add(layer)

    # Trim layers to avoid empty ones
    final_layers = {k: v for k, v in sorted(smeta.items()) if k in used_layers}

    with open(out_fn, "w") as f:
        json.dump(
            {
                "created": timestamp,
                "labels": labels,
                "layers": final_layers,
                "mapping": dict(sorted(ostree_out.items())),
            },
            f,
            indent=2,
        )


def run_with_ostree_files(
    repo: str,
    file_map: dict[str, str],
    fns: Sequence[str],
    callback: Callable[[str], Any],
):
    with tempfile.TemporaryDirectory() as dir:
        for fn in fns:
            if fn in file_map:
                hash = file_map[fn]
                cmd = [
                    "cp",
                    f"{repo}/objects/{hash[:2]}/{hash[2:]}.file",
                    os.path.join(dir, os.path.basename(fn)),
                ]
                if os.getuid() != 0:
                    cmd = ["sudo", *cmd]
                subprocess.run(
                    cmd,
                    check=True,
                )
            else:
                logger.warning(f"File not found in OSTree: {fn}")
                raise FileNotFoundError(fn)

        return callback(dir)


if __name__ == "__main__":
    import sys

    print(sum(get_ostree_map(sys.argv[1], sys.argv[2])[-1].values()))
