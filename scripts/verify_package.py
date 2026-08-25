"""Verify that a freshly built wheel exposes Wingmen's console entry points."""

from __future__ import annotations

from pathlib import Path
from subprocess import run
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="wingmen-wheel-") as output_dir:
        run(
            [
                "uv",
                "build",
                "--wheel",
                "--python",
                str(project_root / ".venv" / "bin" / "python"),
                "--out-dir",
                output_dir,
            ],
            cwd=project_root,
            check=True,
        )
        wheel = next(Path(output_dir).glob("wingmen-*.whl"))
        with ZipFile(wheel) as archive:
            packaged_paths = archive.namelist()
            entry_points_path = next(
                path
                for path in packaged_paths
                if path.endswith(".dist-info/entry_points.txt")
            )
            entry_points = archive.read(entry_points_path).decode("utf-8")

            forbidden_paths = [
                path
                for path in packaged_paths
                if path.startswith("toad/")
                or path.endswith("data/sounds/turn-over.wav")
            ]
            if forbidden_paths:
                raise SystemExit(
                    "wheel contains removed compatibility or completion-audio files:\n"
                    + "\n".join(forbidden_paths)
                )

        install_dir = Path(output_dir) / "venv"
        run(
            [
                "uv",
                "venv",
                "--python",
                str(project_root / ".venv" / "bin" / "python"),
                str(install_dir),
            ],
            check=True,
        )
        python = install_dir / "bin" / "python"
        executables = [install_dir / "bin" / "wingmen"]
        run(["uv", "pip", "install", "--python", str(python), str(wheel)], check=True)
        if (install_dir / "bin" / "wingwomen").exists():
            raise SystemExit("wheel installed the unsupported wingwomen executable")
        for executable in executables:
            run([str(executable), "--version"], check=True)
            run([str(executable), "--help"], check=True)
        smoke = (
            "import asyncio\n"
            "from wingmen.app import WingmenApp\n"
            "async def smoke():\n"
            " async with WingmenApp(setup_prompt=False).run_test(size=(100, 30)) as pilot:\n"
            "  await pilot.pause(0.05)\n"
            "asyncio.run(smoke())"
        )
        run([str(python), "-c", smoke], check=True)

    expected = {"wingmen = wingmen.cli:main"}
    console_scripts = {
        line.strip() for line in entry_points.splitlines() if " = " in line
    }
    if console_scripts != expected or "toad" in entry_points:
        raise SystemExit(
            "wheel has invalid console scripts; expected "
            f"{sorted(expected)!r}, got:\n{entry_points}"
        )


if __name__ == "__main__":
    main()
