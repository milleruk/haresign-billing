"""The templates and the stylesheet must agree on what exists.

This suite exists because they did not. Every page in this service was written
against a design-system vocabulary — `hs-heading`, `hs-lede`, `hs-columns`,
`hs-list`, `hs-datalist` — that the stylesheet never defined. Nothing failed:
Django renders unknown classes happily and CSS ignores them, so the pages looked
structurally right in the markup and rendered as unstyled documents.

The visible symptoms were all the same bug. An `<h1>` with no matching class
falls back to the element rule and takes the *marketing hero* scale, so an
organisation's name rendered at up to 82px. A `<ul>` with no matching class
keeps the browser's default marker and padding, so bullets hung outside the
panel that contained them. A two-column layout whose grid class did not exist
collapsed into one long column.

A class name is a contract between a template and a stylesheet, and it is the
only contract in this codebase that nothing was checking.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

REPO_ROOT = Path(settings.BASE_DIR)
TEMPLATE_DIRECTORIES = sorted(REPO_ROOT.glob('*/templates'))
STYLESHEETS = sorted((REPO_ROOT / 'web' / 'static' / 'css').glob('*.css'))

# `class="..."` values, including those built across a Django tag.
CLASS_ATTRIBUTE = re.compile(r'class="([^"]*)"')
# A bare word is a class only if it is one of ours; Django template syntax and
# conditional fragments inside the attribute are skipped by the `hs-` filter.
# Hyphens are part of a block name (`hs-page-header__title`), so the character
# class has to allow them — an earlier version of this pattern did not, and
# silently skipped most of the vocabulary it was written to check.
HS_CLASS = re.compile(r'^hs-[a-z0-9_-]+$')
CSS_CLASS = r'(hs-[a-z0-9_-]+)'


def defined_classes() -> set[str]:
    defined = set()
    for stylesheet in STYLESHEETS:
        text = stylesheet.read_text(encoding='utf-8')
        defined.update(re.findall(rf'\.{CSS_CLASS}', text))
    return defined


def used_classes() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for directory in TEMPLATE_DIRECTORIES:
        for template in directory.rglob('*.html'):
            text = template.read_text(encoding='utf-8')
            for attribute in CLASS_ATTRIBUTE.findall(text):
                for token in attribute.split():
                    if HS_CLASS.match(token):
                        used.setdefault(token, set()).add(str(template.relative_to(REPO_ROOT)))
    return used


class DesignSystemVocabularyTests(SimpleTestCase):
    def test_the_stylesheets_and_templates_were_found(self):
        """A silent zero here would make every other test in this file pass."""
        self.assertTrue(STYLESHEETS)
        self.assertTrue(TEMPLATE_DIRECTORIES)
        self.assertTrue(used_classes())

    def test_every_class_a_template_uses_is_defined_in_the_stylesheet(self):
        """The contract this suite exists to enforce."""
        defined = defined_classes()
        offenders = {
            name: sorted(templates)
            for name, templates in sorted(used_classes().items())
            if name not in defined
        }
        self.assertEqual(offenders, {})

    def test_no_page_heading_falls_back_to_the_marketing_hero_scale(self):
        """An application page must size its own heading.

        `h1 { font-size: var(--hs-heading-hero) }` is right for a landing page
        and wrong for a working screen, so any class a template puts on an `h1`
        has to carry a size of its own.
        """
        stylesheet = '\n'.join(sheet.read_text(encoding='utf-8') for sheet in STYLESHEETS)
        heading_classes = set()
        for directory in TEMPLATE_DIRECTORIES:
            for template in directory.rglob('*.html'):
                text = template.read_text(encoding='utf-8')
                for attribute in re.findall(r'<h1[^>]*class="([^"]*)"', text):
                    heading_classes.update(
                        token for token in attribute.split() if HS_CLASS.match(token)
                    )
        self.assertTrue(heading_classes, 'no <h1> carried a design-system class')
        for name in sorted(heading_classes):
            with self.subTest(heading_class=name):
                block = re.search(rf'\.{re.escape(name)}\s*\{{(.*?)\}}', stylesheet, re.DOTALL)
                self.assertIsNotNone(block, f'.{name} is not defined')
                self.assertIn('font-size', block.group(1), f'.{name} sets no font-size')

    def test_every_list_class_removes_the_default_marker(self):
        """A bullet outside its panel was one of the reported defects.

        A `<ul>` used as a list of records rather than of sentences must clear
        both the marker and the browser's default left padding — clearing only
        the marker leaves the indent, and clearing neither hangs the content
        into the panel's border.
        """
        stylesheet = '\n'.join(sheet.read_text(encoding='utf-8') for sheet in STYLESHEETS)
        list_classes = set()
        for directory in TEMPLATE_DIRECTORIES:
            for template in directory.rglob('*.html'):
                text = template.read_text(encoding='utf-8')
                for attribute in re.findall(r'<ul[^>]*class="([^"]*)"', text):
                    list_classes.update(
                        token for token in attribute.split() if HS_CLASS.match(token)
                    )
        self.assertTrue(list_classes, 'no <ul> carried a design-system class')
        for name in sorted(list_classes):
            with self.subTest(list_class=name):
                # A modifier inherits its base block's reset, so `hs-grid--tight`
                # is satisfied by whatever `hs-grid` already does. Checking the
                # modifier alone would demand every variant repeat the rule.
                candidates = [name]
                if '--' in name:
                    candidates.append(name.split('--', 1)[0])
                declarations = ''
                for candidate in candidates:
                    block = re.search(
                        rf'\.{re.escape(candidate)}\s*\{{(.*?)\}}', stylesheet, re.DOTALL
                    )
                    if block:
                        declarations += block.group(1)
                self.assertTrue(declarations, f'.{name} is not defined')
                self.assertTrue(
                    'list-style' in declarations or 'padding-left' in declarations,
                    f'.{name} neither removes the marker nor resets the indent',
                )
