"""Тесты фильтров Jinja2 из ``app.web.templating``.

Проверяют автолинковку ``linkify``: ссылки в домашнем задании, плане и
примечании должны становиться кликабельными, а сам фильтр обязан
сохранять экранирование HTML (XSS через поле занятия невозможен).
"""

from __future__ import annotations

from app.web.templating import linkify


class TestLinkify:
    """Проверки превращения ссылок в тексте в HTML-теги ``<a>``."""

    def test_plain_text_without_links_is_untouched(self) -> None:
        """Текст без ссылок возвращается как есть."""
        assert str(linkify("Решить 10 задач, стр. 24")) == "Решить 10 задач, стр. 24"

    def test_none_and_empty_give_empty_markup(self) -> None:
        """None и пустая строка не ломают шаблон."""
        assert str(linkify(None)) == ""
        assert str(linkify("")) == ""

    def test_https_url_becomes_link(self) -> None:
        """Полный адрес превращается в ссылку, открывающуюся в новой вкладке."""
        markup = str(linkify("Задачи: https://lichess.org/puzzles"))

        assert '<a href="https://lichess.org/puzzles"' in markup
        assert 'target="_blank"' in markup
        assert 'rel="noopener noreferrer"' in markup
        assert ">https://lichess.org/puzzles</a>" in markup
        assert "Задачи: " in markup

    def test_bare_domain_with_path_becomes_link(self) -> None:
        """Адрес без схемы (lichess.org/puzzles) тоже становится ссылкой."""
        markup = str(linkify("Смотри lichess.org/practice"))

        assert '<a href="https://lichess.org/practice"' in markup

    def test_www_url_gets_scheme(self) -> None:
        """Адрес с «www.» получает схему https в href."""
        markup = str(linkify("Видео: www.youtube.com/watch?v=abc"))

        assert '<a href="https://www.youtube.com/watch?v=abc"' in markup

    def test_trailing_punctuation_stays_out_of_href(self) -> None:
        """Точка в конце предложения не попадает в адрес."""
        markup = str(linkify("Открой chess.com/tactics."))

        assert '<a href="https://chess.com/tactics"' in markup
        assert "</a>." in markup

    def test_balanced_parentheses_kept_in_url(self) -> None:
        """Парные скобки внутри адреса сохраняются, внешние — нет."""
        markup = str(linkify("Ссылка (https://example.com/abc_(def)) тут"))

        assert '<a href="https://example.com/abc_(def)"' in markup

    def test_numbers_with_dots_are_not_links(self) -> None:
        """Числа с точкой (10.5) и сокращения (стр. 24) ссылками не считаются."""
        markup = str(linkify("Стр. 24, задачи 10.5 и 12.7"))

        assert "<a " not in markup

    def test_bare_domain_without_path_is_not_link(self) -> None:
        """Одинокое доменное имя без пути не превращается в ссылку."""
        markup = str(linkify("Задачник из серии chess.com"))

        assert "<a " not in markup

    def test_html_is_escaped(self) -> None:
        """HTML из данных экранируется — XSS через поле занятия невозможен."""
        markup = str(linkify('<script>alert("xss")</script>'))

        assert "<script>" not in markup
        assert "&lt;script&gt;" in markup

    def test_ampersand_in_query_is_escaped(self) -> None:
        """Амперсанд в параметрах ссылки экранируется, адрес остаётся рабочим."""
        markup = str(linkify("https://example.com/a?b=1&c=2"))

        assert 'href="https://example.com/a?b=1&amp;c=2"' in markup

    def test_multiline_text_keeps_newlines(self) -> None:
        """Переносы строк сохраняются: шаблоны рисуют их через CSS."""
        markup = str(linkify("Первая строка\nhttps://lichess.org/\nТретья"))

        assert "\n" in markup
        assert markup.count("<a ") == 1

    def test_several_links_in_one_text(self) -> None:
        """В одном абзаце находится несколько ссылок."""
        markup = str(linkify("Теория: lichess.org/practice, видео youtube.com/watch?v=1"))

        assert markup.count("<a ") == 2
