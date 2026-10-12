"""requirements.txt and requirements-build.txt pin every package, including the ones pip
pulls in by itself, at the versions installed (and tested) here. Offline: it reads the
installed packages' own metadata, never the internet."""

import importlib.metadata as metadata
import re
import unittest
from pathlib import Path

try:
    from packaging.requirements import Requirement
except ImportError:  # packaging comes with the build tools; pip always carries its own copy
    from pip._vendor.packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
APP_FILE = ROOT / "requirements.txt"
BUILD_FILE = ROOT / "requirements-build.txt"


def key(name):
    """Package names compare like pip compares them: "typing_extensions" is "typing-extensions"."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(path):
    """{name: version} for every name==version line in the file."""
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = line.partition("==")
        if not sep:
            raise AssertionError(f"{path.name}: not an exact pin: {line!r}")
        found[key(name)] = version.strip()
    return found


def installed(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def needs(name):
    """The packages name needs on this PC (its requirements whose conditions hold here, no extras)."""
    result = []
    for text in metadata.requires(name) or []:
        requirement = Requirement(text)
        if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
            result.append(key(requirement.name))
    return result


class RequirementsTests(unittest.TestCase):
    def check_file(self, path, also_allowed=()):
        pinned = pins(path)
        for name, version in pinned.items():
            with self.subTest(file=path.name, package=name):
                self.assertEqual(installed(name), version)
        for name in pinned:
            for needed in needs(name):
                with self.subTest(file=path.name, package=name, needs=needed):
                    self.assertTrue(needed in pinned or needed in also_allowed,
                                    f"{name} needs {needed}, which isn't pinned in {path.name}")
        return pinned

    def test_every_app_package_is_pinned_at_its_installed_version(self):
        self.check_file(APP_FILE)

    def test_every_build_package_is_pinned_at_its_installed_version(self):
        if installed("pyinstaller") is None:
            self.skipTest("the build tools aren't installed")
        self.check_file(BUILD_FILE, also_allowed=pins(APP_FILE))

    def test_only_exact_pins(self):
        for path in (APP_FILE, BUILD_FILE):
            with self.subTest(file=path.name):
                pins(path)  # raises on a line that isn't name==version


if __name__ == "__main__":
    unittest.main()
