from pathlib import Path
import tempfile
import unittest

from wingmen.prompt.resource import (
    MAX_RESOURCE_BYTES,
    ResourceNotRelative,
    ResourceTooLarge,
    load_resource,
)


class PromptResourceTests(unittest.TestCase):
    def test_resource_loader_rejects_oversized_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as project:
            root = Path(project)
            large = root / "large.txt"
            large.write_bytes(b"x" * (MAX_RESOURCE_BYTES + 1))

            with self.assertRaises(ResourceTooLarge):
                load_resource(root, Path("large.txt"))

    def test_resource_loader_rejects_traversal_and_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as other:
            root = Path(project)
            outside = Path(other) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")

            for path in (Path("../outside.txt"), outside):
                with self.subTest(path=path):
                    with self.assertRaises(ResourceNotRelative):
                        load_resource(root, path)

    def test_resource_loader_rejects_symlinks_outside_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as other:
            root = Path(project)
            outside = Path(other) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            (root / "outside-link").symlink_to(outside)

            with self.assertRaises(ResourceNotRelative):
                load_resource(root, Path("outside-link"))

    def test_resource_loader_reads_a_project_file(self) -> None:
        with tempfile.TemporaryDirectory() as project:
            root = Path(project)
            (root / "inside.txt").write_text("safe", encoding="utf-8")

            resource = load_resource(root, Path("inside.txt"))
            self.assertEqual(resource.text, "safe")
            self.assertEqual(resource.path, (root / "inside.txt").resolve())


if __name__ == "__main__":
    unittest.main()
