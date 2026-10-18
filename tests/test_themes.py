"""Generated Flume palettes work without an injected Python bridge."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bronson import themes
from bronson.cache import Cache
from bronson.cli import ActionsDashboard


def palette(name, background, error, dark=True):
    return {
        "name": name,
        "dark": dark,
        **dict.fromkeys(themes._THEME_COLORS, background),
        "error": error,
        "roles": {
            **dict.fromkeys(
                (
                    "text",
                    "text_subtle",
                    "text_muted",
                    "accent",
                    "accent_secondary",
                    "accent_tertiary",
                    "warning",
                    "success",
                ),
                "#73a6b6",
            ),
            "error": error,
        },
        "variables": {
            **dict.fromkeys(
                (
                    "scrollbar",
                    "scrollbar_hover",
                    "scrollbar_active",
                    "scrollbar_background",
                    "selection_foreground",
                    "cursor",
                    "border_focus",
                    "border",
                ),
                background,
            ),
            "selection_background": "#73a6b6 30%",
        },
    }


class ThemeFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = dict(os.environ)
        for key in (
            "BRONSON_FLUME_DIR",
            "FLUME_TRACKER_THEME_DIR",
            "FLUME_SCHEMA_FILE",
            "BRONSON_THEME",
        ):
            env.pop(key, None)
        env.update(
            HOME=str(self.root),
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_DATA_HOME=str(self.root / "data"),
        )
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        themes._ROLES.clear()
        self.addCleanup(themes._ROLES.clear)
        self.extras = self.root / "extras"
        self.palettes = self.extras / "tracker-tui"
        self.palettes.mkdir(parents=True)
        config = self.root / "config" / "bronson"
        config.mkdir(parents=True)
        (config / "flume").symlink_to(self.extras, target_is_directory=True)
        for name, background, error, dark in (
            ("dusk", "#232136", "#ee7583", True),
            ("opal", "#ded4d1", "#bc3945", False),
        ):
            (self.palettes / f"flume-{name}.json").write_text(
                json.dumps(palette(f"flume-{name}", background, error, dark))
            )
            target = self.extras / f"set-{name}"
            target.mkdir()
            (target / "schema").write_text(name + "\n")
        self.activate("dusk")

    def activate(self, name):
        staged = self.extras / ".current-stage"
        staged.symlink_to(f"set-{name}", target_is_directory=True)
        staged.replace(self.extras / "current")


class ThemeTests(ThemeFixture, unittest.TestCase):
    def test_discovers_config_link_and_preserves_exact_colors(self):
        loaded = themes.load_flume_themes()
        self.assertEqual([theme.name for theme in loaded], ["flume-dusk", "flume-opal"])
        self.assertEqual(loaded[0].background, "#232136")
        self.assertEqual(loaded[0].variables["text-error"], "#ee7583")
        self.assertEqual(loaded[0].variables["screen-selection-background"], "#73a6b6 30%")
        self.assertEqual(loaded[0].luminosity_spread, 0.0)
        self.assertEqual(loaded[0].text_alpha, 1.0)
        self.assertEqual(themes.theme_roles("flume-opal")["error"], "#bc3945")
        self.assertEqual(themes.active_flume_theme(), "flume-dusk")

    def test_separate_generated_palettes_follow_shared_schema(self):
        generated = self.root / "generated"
        self.palettes.rename(generated)
        configured = self.root / "config" / "bronson" / "flume-themes"
        configured.symlink_to(generated, target_is_directory=True)
        self.assertEqual(len(themes.load_flume_themes()), 2)
        self.assertEqual(themes.active_flume_theme(), "flume-dusk")
        self.activate("opal")
        self.assertEqual(themes.active_flume_theme(), "flume-opal")
        os.environ["FLUME_TRACKER_THEME_DIR"] = str(self.root / "missing")
        self.assertEqual(themes.load_flume_themes(), [])

    def test_reads_live_semantic_exports_before_legacy_generated_palettes(self):
        directory = self.extras / "palette"
        directory.mkdir()
        data = {
            "format_version": 1,
            "schema": "dusk",
            "appearance": "dark",
            "colors": {
                **dict.fromkeys(
                    (
                        "text",
                        "fg",
                        "syntax_comment",
                        "border",
                        "border_variant",
                        "accent",
                        "syntax_keyword",
                        "cyan",
                        "success",
                        "warning",
                        "error",
                        "bg",
                        "surface_alt",
                        "surface",
                        "border_focused",
                        "element_active",
                        "on_accent",
                    ),
                    "#222222",
                ),
                "bg": "#232136",
                "text": "#eeeeee",
                "error": "#ee7583",
            },
        }
        path = directory / "flume-dusk.json"
        path.write_text(json.dumps(data))
        loaded = themes.load_flume_themes()
        self.assertEqual([theme.name for theme in loaded], ["flume-dusk"])
        self.assertEqual(loaded[0].background, "#232136")
        self.assertEqual(loaded[0].variables["text"], "#eeeeee")
        self.assertEqual(themes.theme_roles("flume-dusk")["error"], "#ee7583")
        self.assertEqual(themes.active_flume_theme(), "flume-dusk")

        data["colors"]["bg"] = "#ded4d1"
        data["appearance"] = "light"
        path.write_text(json.dumps(data))
        changed = themes.load_flume_themes()[0]
        self.assertEqual(changed.background, "#ded4d1")
        self.assertFalse(changed.dark)
        for key, value in (("format_version", 2), ("format_version", True), ("colors", [])):
            path.write_text(json.dumps({**data, key: value}))
            self.assertEqual(themes.load_flume_themes(), [])
        data["colors"]["bg"] = "invalid-color"
        path.write_text(json.dumps(data))
        self.assertEqual(themes.load_flume_themes(), [])
        self.assertIsNone(themes.active_flume_theme())

    def test_deeply_nested_json_is_ignored(self):
        path = self.palettes / "flume-dusk.json"
        path.write_text("[" * 2000 + "0" + "]" * 2000)
        loaded = themes.load_flume_themes()
        self.assertEqual([theme.name for theme in loaded], ["flume-opal"])
        self.assertIsNone(themes.theme_roles("flume-dusk"))

    def test_missing_or_invalid_palettes_fall_back_safely(self):
        (self.palettes / "flume-bad.json").write_text("[]")
        invalid = palette("flume-invalid", "not-a-color", "#ee7583")
        (self.palettes / "flume-invalid.json").write_text(json.dumps(invalid))
        self.assertEqual(len(themes.load_flume_themes()), 2)
        os.environ["BRONSON_FLUME_DIR"] = str(self.root / "missing")
        self.assertEqual(themes.load_flume_themes(), [])
        self.assertIsNone(themes.active_flume_theme())
        self.assertIsNone(themes.theme_roles("flume-dusk"))

    def test_explicit_and_legacy_paths_are_supported(self):
        os.environ["BRONSON_FLUME_DIR"] = str(self.extras)
        self.assertEqual(len(themes.load_flume_themes()), 2)
        os.environ["BRONSON_FLUME_DIR"] = str(self.root / "missing")
        os.environ["FLUME_TRACKER_THEME_DIR"] = str(self.palettes)
        os.environ["FLUME_SCHEMA_FILE"] = str(self.extras / "current" / "schema")
        self.assertEqual(len(themes.load_flume_themes()), 2)
        self.assertEqual(themes.active_flume_theme(), "flume-dusk")

    def test_discovers_standard_lazy_checkout_without_config_link(self):
        (self.root / "config" / "bronson" / "flume").unlink()
        plugin = self.root / "data" / "nvim" / "lazy" / "flume.nvim"
        plugin.mkdir(parents=True)
        (plugin / "extras").symlink_to(self.extras, target_is_directory=True)
        self.assertEqual(len(themes.load_flume_themes()), 2)
        self.assertEqual(themes.active_flume_theme(), "flume-dusk")

    def test_invalid_utf8_schema_is_ignored(self):
        themes.load_flume_themes()
        (self.extras / "current" / "schema").write_bytes(b"\xff")
        self.assertIsNone(themes.active_flume_theme())

    def test_unknown_active_palette_is_not_applied(self):
        themes.load_flume_themes()
        self.activate("missing")
        self.assertIsNone(themes.active_flume_theme())


class LiveThemeTests(ThemeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_follows_atomic_current_swap(self):
        themes.load_flume_themes()
        changes = themes.watch_flume_theme_changes()
        pending = asyncio.create_task(anext(changes))
        try:
            await asyncio.sleep(0.15)
            self.activate("opal")
            self.assertEqual(await asyncio.wait_for(pending, 3), "flume-opal")
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await changes.aclose()

    async def test_follows_atomic_swap_with_separate_generated_palettes(self):
        generated = self.root / "generated"
        self.palettes.rename(generated)
        configured = self.root / "config" / "bronson" / "flume-themes"
        configured.symlink_to(generated, target_is_directory=True)
        await self.test_follows_atomic_current_swap()

    async def test_follows_standalone_schema_override(self):
        themes.load_flume_themes()
        directory = self.root / "custom"
        directory.mkdir()
        schema = directory / "schema"
        schema.write_text("dusk\n")
        os.environ["FLUME_SCHEMA_FILE"] = str(schema)
        changes = themes.watch_flume_theme_changes()
        pending = asyncio.create_task(anext(changes))
        try:
            await asyncio.sleep(0.15)
            staged = directory / ".schema-stage"
            staged.write_text("opal\n")
            staged.replace(schema)
            self.assertEqual(await asyncio.wait_for(pending, 3), "flume-opal")
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await changes.aclose()

    async def test_dashboard_follows_flume_but_respects_manual_override(self):
        app = ActionsDashboard("owner/repo", Cache(self.root / "cache"))
        with patch.object(app, "poll"):
            async with app.run_test() as pilot:
                self.assertEqual(app.theme, "flume-dusk")
                self.assertEqual(app.color("error", "red"), "#ee7583")
                await pilot.pause(0.15)
                self.activate("opal")
                for _ in range(30):
                    if app.theme == "flume-opal":
                        break
                    await pilot.pause(0.1)
                self.assertEqual(app.theme, "flume-opal")
                self.assertEqual(app.color("error", "red"), "#bc3945")
                app.select_theme("textual-dark")
                self.activate("dusk")
                await pilot.pause(0.4)
                self.assertEqual(app.theme, "textual-dark")
                app.select_theme("auto")
                self.assertEqual(app.theme, "flume-dusk")


if __name__ == "__main__":
    unittest.main()
