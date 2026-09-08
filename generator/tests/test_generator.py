import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generate import (
    RenderContext,
    parse_insert_rows,
    parse_bbcode,
    render_nodes,
    safe_url,
    select_forum_ids,
)


class SqlParserTests(unittest.TestCase):
    def test_parses_mysql_escapes_binary_and_null(self):
        sql = (
            "INSERT INTO `xf_post` (`post_id`, `message`, `blob`) VALUES\n"
            "\t(1, 'Ala\\nma \\'kota\\'', _binary 0x6869),\n"
            "\t(2, NULL, _binary 'tekst');"
        )
        table, rows = parse_insert_rows(sql)
        self.assertEqual(table, "xf_post")
        self.assertEqual(rows[0]["message"], "Ala\nma 'kota'")
        self.assertEqual(rows[0]["blob"], "hi")
        self.assertIsNone(rows[1]["message"])


class BbCodeTests(unittest.TestCase):
    def setUp(self):
        self.context = RenderContext({}, {})

    def render(self, source):
        return render_nodes(parse_bbcode(source), self.context)

    def test_escapes_raw_html(self):
        rendered = self.render('<script>alert("x")</script>')
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_rejects_javascript_url(self):
        rendered = self.render("[url=javascript:alert(1)]klik[/url]")
        self.assertNotIn("href=", rendered)
        self.assertIn("klik", rendered)

    def test_external_link_is_isolated(self):
        rendered = self.render("[url=https://example.com]link[/url]")
        self.assertIn('rel="nofollow noopener noreferrer"', rendered)

    def test_quotes_and_lists_are_semantic(self):
        rendered = self.render('[quote="Ala"]tekst[/quote][list][*]jeden[*]dwa[/list]')
        self.assertIn("<blockquote>", rendered)
        self.assertIn("<ul><li>jeden</li><li>dwa</li></ul>", rendered)

    def test_youtube_uses_privacy_host(self):
        rendered = self.render("[youtube]dQw4w9WgXcQ[/youtube]")
        self.assertIn("youtube-nocookie.com/embed/dQw4w9WgXcQ", rendered)

    def test_unclosed_image_tag_stays_text(self):
        rendered = self.render('przykład [img] oraz dalsza część')
        self.assertNotIn("<img", rendered)
        self.assertIn("[img]", rendered)

    def test_safe_url(self):
        self.assertEqual(safe_url("https://example.com"), "https://example.com")
        self.assertIsNone(safe_url("data:text/html,boom"))


class ForumSelectionTests(unittest.TestCase):
    def test_excludes_private_forum_case_insensitively(self):
        forums = {5: {}, 12: {}}
        nodes = {5: {"title": "Laboratorium X-21"}, 12: {"title": "Reaktor"}}
        self.assertEqual(select_forum_ids(forums, nodes, ["reaktor"]), {5})


if __name__ == "__main__":
    unittest.main()
