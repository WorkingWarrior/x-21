import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generate import (
    RenderContext,
    parse_insert_rows,
    pl_count,
    parse_bbcode,
    render_nodes,
    safe_url,
    select_forum_ids,
    post_excerpt,
    user_page_file,
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

    def test_xenforo_image_attributes_are_supported(self):
        rendered = self.render('[IMG width="290px"]https://example.com/image.jpg[/IMG]')
        self.assertIn('<img src="https://example.com/image.jpg"', rendered)
        self.assertIn(' width="290"', rendered)

    def test_xenforo_attachment_attributes_are_supported(self):
        context = RenderContext(
            {20: {"filename": "example.jpg", "width": 640, "height": 480}},
            {20: "media/attachments/0/example.jpg"},
        )
        rendered = render_nodes(parse_bbcode('[ATTACH type="full"]20[/ATTACH]'), context)
        self.assertIn('src="media/attachments/0/example.jpg"', rendered)
        self.assertNotIn("[ATTACH", rendered)

    def test_safe_url(self):
        self.assertEqual(safe_url("https://example.com"), "https://example.com")
        self.assertIsNone(safe_url("data:text/html,boom"))


class ForumSelectionTests(unittest.TestCase):
    def test_excludes_private_forum_case_insensitively(self):
        forums = {5: {}, 12: {}}
        nodes = {5: {"title": "Laboratorium X-21"}, 12: {"title": "Reaktor"}}
        self.assertEqual(select_forum_ids(forums, nodes, ["reaktor"]), {5})


class UserProfileTests(unittest.TestCase):
    def test_profile_page_names(self):
        self.assertEqual(user_page_file(7), "user_7.html")
        self.assertEqual(user_page_file(7, 2), "user_7_page_2.html")

    def test_post_excerpt_removes_bbcode(self):
        self.assertEqual(post_excerpt("[b]Ala[/b]\nma kota"), "Ala ma kota")

    def test_polish_plural_forms(self):
        self.assertEqual(pl_count(1, "post", "posty", "postów"), "1 post")
        self.assertEqual(pl_count(2, "post", "posty", "postów"), "2 posty")
        self.assertEqual(pl_count(12, "post", "posty", "postów"), "12 postów")
        self.assertEqual(pl_count(21, "post", "posty", "postów"), "21 postów")
        self.assertEqual(pl_count(22, "post", "posty", "postów"), "22 posty")


if __name__ == "__main__":
    unittest.main()
