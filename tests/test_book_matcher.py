"""Тесты распознавания книг, даты публикации и отзывов продавца."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from books.catalog import get_catalog
from books.filters import (
    MOSCOW,
    is_fresh,
    is_wholesale_seller,
    parse_posting_date,
    parse_reviews_count,
)
from books.matcher import MEDIUM, STRONG, WEAK, match_text


def indexes(text: str) -> list[int]:
    return [m.book.index for m in match_text(text).matches]


def only(text: str):
    report = match_text(text)
    assert len(report.matches) == 1, [m.book.title for m in report.matches]
    return report.matches[0]


class CatalogTest(unittest.TestCase):
    def test_catalog_has_74_positions(self):
        catalog = get_catalog()
        self.assertEqual(len(catalog), 74)
        self.assertEqual([book.index for book in catalog], list(range(1, 75)))

    def test_prices_and_series(self):
        catalog = {book.index: book for book in get_catalog()}
        self.assertEqual(catalog[1].series, "lp")
        self.assertEqual(catalog[1].year, 1948)
        self.assertEqual(catalog[1].price_min, 4500)
        self.assertEqual(catalog[1].price_max, 6500)
        self.assertEqual(catalog[1].price_top, 12000)
        self.assertEqual(catalog[33].series, "academia")
        self.assertEqual(catalog[33].title, "Морфология сказки")
        self.assertEqual(catalog[33].price_min, 15000)
        # New LP ext entries have series=lp and no prices
        for idx in (69, 70, 71, 72, 73, 74):
            self.assertEqual(catalog[idx].series, "lp")
            self.assertIsNone(catalog[idx].price_min)
            self.assertIsNone(catalog[idx].price_max)
            self.assertIsNone(catalog[idx].price_top)


class PositiveMatchTest(unittest.TestCase):
    def test_lp_books(self):
        cases = [
            ("Цезарь. Записки о Галльской войне. 1948. Наука", 1),
            ("Афанасий Никитин. Хождение за три моря. 1948", 2),
            ("Низам ал-Мульк. Сиасет-намэ. 1949", 3),
            ("Цицерон. Письма к Аттику, т. I. 1949", 4),
            ("Катон. Земледелие. 1950. Наука", 5),
            ("Махабхарата. Кн. 1. Адипарва. 1950", 6),
            ("Плиний Младший. Письма. 1950", 7),
            ("Повесть временных лет, ч. 1. 1950", 8),
            ("Слово о полку Игореве. 1950. Наука", 9),
            ("Послания Ивана Грозного. 1951", 10),
            ("Ксенофонт. Анабасис. 1951", 11),
            ("Лукан. Фарсалия. 1951", 12),
            ("Посошков. Книга о скудости и богатстве. 1951", 13),
            ("Временник Ивана Тимофеева. 1951", 14),
            ("Ла Боэси. Рассуждение о добровольном рабстве. 1952", 15),
            ("Кабус-намэ. 1953", 16),
            ("Симеон Полоцкий. Избранные сочинения. 1953", 17),
            ("Монтень. Опыты, кн. 1. 1954", 18),
            ("Шицзин. 1957", 19),
            ("Артхашастра. 1959", 20),
            ("Дигенис Акрит. 1960", 21),
            ("Книга моего деда Коркута. 1962", 22),
            ("Махабхарата. Кн. 2. Сабхапарва. 1962", 23),
            ("Старшая Эдда. 1963", 24),
            ("Эпос сербского народа. 1963", 25),
            ("Крижанич. Политика. 1965", 26),
            # Новые LP-издания (69–74): том/глава определяют позицию однозначно
            ("Марк Аврелий. Размышления. 1985. Наука. Ленинград", 69),
            ("Марк Аврелий Антонин. Размышления. 1993. Наука. СПб. 2-е издание", 70),
            ("Цицерон. Письма к Аттику, близким, брату Квинту. Т. II. 1950. Наука", 71),
            ("Цицерон. Письма к Аттику, близким, брату Квинту. Т. III. 1951. Наука", 72),
            ("Махабхарата. Кн. 4. Виратапарва. 1967. Наука", 73),
            ("Махабхарата. Кн. 5. Удьйогапарва. 1976. Наука. Ленинград", 74),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(indexes(text), [expected])

        # Том цицероновых «Писем» определяется приметой издания: том I
        # (1949) не должен вылезать рядом с томом II/III.
        by_idx = {
            m.book.index: m
            for m in match_text(
                "Цицерон. Письма к Аттику. Т. II. 1950. Наука").matches
        }
        self.assertIn("примета издания", by_idx[71].evidence)
        self.assertNotIn(4, by_idx)

    def test_academia_books(self):
        cases = [
            ("Платон. Полное собрание творений, т. 1. 1923. Academia", 27),
            ("Тынянов. Проблема стихотворного языка. 1924", 28),
            ("Фрейд. Я и Оно. 1924", 29),
            ("Анри де Ренье. Собрание сочинений, т. 1. 1925", 30),
            ("Аристотель. Поэтика. 1927", 31),
            ("Боккаччо. Декамерон, т. 1. 1927", 32),
            ("Пропп. Морфология сказки. 1928. Academia", 33),
            ("Свифт. Путешествия Гулливера. 1928", 34),
            ("Апулей. Золотой осел. 1929", 35),
            ("Катулл. Книга лирики. 1929", 36),
            ("Сервантес. Дон Кихот, т. 1. 1929", 37),
            ("Дефо. Робинзон Крузо. 1929", 38),
            ("Книга тысячи и одной ночи, т. 1. 1929", 39),
            ("Аристофан. Книга комедий. 1930", 40),
            ("Эразм Роттердамский. Похвальное слово глупости. 1931", 41),
            ("Жизнь Бенвенуто Челлини. 1931", 42),
            ("Гелиодор. Эфиопика. 1932", 43),
            ("Прево. Манон Леско. 1932", 44),
            ("Русская крестьянская игрушка. 1933", 45),
            ("Слово о полку Игореве. 1934. Палех. Голиков", 46),
            ("Фацетии Поджо Браччолини. 1934", 47),
            ("Сасанидский металл. 1935", 48),
            ("Фаюмский портрет. 1936", 49),
            ("Вико. Основания новой науки об общей природе наций. 1937", 50),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(indexes(text), [expected])

    def test_confidence_strong_with_year(self):
        self.assertEqual(only("Пропп. Морфология сказки. 1928. Academia").confidence, STRONG)

    def test_confidence_medium_with_series_only(self):
        # Общее слово «Наука» без маркера издателя доказательством не является.
        match = only("Цезарь. Записки о Галльской войне. Наука")
        self.assertEqual(match.confidence, WEAK)
        self.assertEqual(match.evidence, ())

    def test_confidence_medium_with_publisher_marker(self):
        match = only("Цезарь. Записки о Галльской войне. Издательство Наука")
        self.assertEqual(match.confidence, MEDIUM)
        self.assertIn("издательство/серия", match.evidence)

    def test_confidence_weak_without_evidence(self):
        match = only("Продаю Записки о Галльской войне, состояние хорошее")
        self.assertEqual(match.confidence, WEAK)

    def test_publisher_in_header_does_not_apply_to_other_positions(self):
        # Издательство из «шапки» лота не относится к книгам в составе.
        text = (
            "Продаю книги издательства Academia, 1930-е годы.\n"
            "В составе: Декамерон, Морфология сказки, Гулливер"
        )
        report = match_text(text)
        by_index = {m.book.index: m for m in report.matches}
        self.assertEqual(sorted(by_index), [32, 33, 34])
        for match in by_index.values():
            self.assertEqual(match.confidence, WEAK)
            self.assertEqual(match.evidence, ())
        self.assertTrue(report.confirmed().is_empty)

    def test_multi_book_lot(self):
        text = (
            "Книги из библиотеки:\n"
            "Цезарь. Записки о Галльской войне. 1948. Наука\n"
            "Пропп. Морфология сказки. 1928. Academia\n"
            "Старшая Эдда. 1963. Наука"
        )
        self.assertEqual(indexes(text), [1, 24, 33])
        report = match_text(text)
        low, high = report.estimate()
        self.assertEqual(low, 4500 + 7000 + 15000)
        self.assertEqual(high, 6500 + 10000 + 22000)

    def test_word_forms_and_yo(self):
        self.assertEqual(indexes("ЗАПИСКИ О ГАЛЛЬСКОЙ ВОЙНЕ 1948"), [1])
        self.assertEqual(indexes("Хожение за три моря 1948"), [2])

    def test_missing_part_is_still_a_match(self):
        self.assertEqual(indexes("Повесть временных лет. 1950"), [8])

    def test_loose_key_with_year(self):
        self.assertEqual(indexes("Махабхарата 1950"), [6])
        self.assertEqual(indexes("Махабхарата 1962"), [23])
        self.assertEqual(indexes("Эдда 1963"), [24])

    def test_negated_bad_term_is_allowed(self):
        self.assertEqual(indexes("Морфология сказки, не репринт, 1928"), [33])
        self.assertEqual(indexes("Фрейд Я и Оно, это не переиздание, 1924"), [29])


class ConfirmedMatchesTest(unittest.TestCase):
    """Совпадение только по названию не считается доказательством."""

    def test_title_only_is_dropped(self):
        report = match_text("Книга Робинзон Крузо, новая").confirmed()
        self.assertTrue(report.is_empty)

    def test_edition_evidence_is_kept(self):
        report = match_text("Робинзон Крузо. 1929. Academia").confirmed()
        self.assertFalse(report.is_empty)

    def test_other_edition_evidence_is_kept(self):
        report = match_text(
            "Записки о Галльской войне. Издательство Наука",
        ).confirmed()
        self.assertFalse(report.is_empty)
        self.assertEqual(report.matches[0].book.index, 1)

    def test_generic_publisher_word_alone_is_not_evidence(self):
        # «Наука» без маркера издателя — слишком общее слово.
        report = match_text("Записки о Галльской войне. Наука").confirmed()
        self.assertTrue(report.is_empty)

    def test_ambiguous_group_without_year_goes_to_llm(self):
        # Неразрешённый дубль без года подтверждается только LLM-проверкой.
        report = match_text("Слово о полку Игореве")
        self.assertTrue(report.needs_year)
        self.assertTrue(report.confirmed().is_empty)
        self.assertEqual(
            sorted(m.book.index for m in report.ambiguous), [9, 46],
        )

    def test_multi_book_lot_keeps_only_confirmed(self):
        text = (
            "Книги одним лотом:\n"
            "Робинзон Крузо, новая\n"
            "Пропп. Морфология сказки. 1928"
        )
        report = match_text(text).confirmed()
        self.assertEqual([m.book.index for m in report.matches], [33])

    def test_year_on_separate_line_counts(self):
        self.assertEqual(indexes("Слово о полку Игореве\n1950 г."), [9])

    def test_year_of_neighbour_line_does_not_leak(self):
        # 1928 относится к «Морфологии сказки», а не к «Робинзону Крузо».
        report = match_text(
            "Книги одним лотом:\n"
            "Робинзон Крузо, новая\n"
            "Пропп. Морфология сказки. 1928"
        )
        by_index = {m.book.index: m for m in report.matches}
        self.assertEqual(by_index[33].confidence, STRONG)
        self.assertEqual(by_index[38].confidence, WEAK)
        self.assertEqual(by_index[38].evidence, ())


class FalsePositiveRegressionTest(unittest.TestCase):
    """Реальные лоты, которые раньше приходили как ложные совпадения."""

    def _assert_not_confirmed(self, text: str, index: int | None = None):
        report = match_text(text)
        self.assertTrue(
            report.confirmed().is_empty,
            f"ожидалось отсутствие подтверждения: {text}",
        )
        if index is not None:
            self.assertIn(index, [m.book.index for m in report.matches])

    def test_khalif_na_chas_is_not_thousand_and_one_nights(self):
        self._assert_not_confirmed(
            "Халиф на час. Из тысячи и одной ночи. Изд. Academia", 39,
        )

    def test_aristotle_collected_works_is_not_poetika(self):
        self._assert_not_confirmed(
            "Полный комплект Сочинений Аристотеля в 4 томах. "
            "Издательство Академия, Ленинград. Поэтика",
            31,
        )

    def test_don_quixote_without_edition_evidence_is_not_confirmed(self):
        self._assert_not_confirmed("Сервантес дон кихот. Академия", 37)

    def test_modern_robinson_crusoe_is_not_confirmed(self):
        self._assert_not_confirmed("Даниэль Дефо - Робинзон Крузо", 38)

    def test_slovo_without_year_is_not_confirmed(self):
        self._assert_not_confirmed("Слово о полку Игореве")

    def test_ambiguous_candidates_are_marked_for_llm(self):
        report = match_text("Слово о полку Игореве")
        self.assertEqual(
            sorted(m.book.index for m in report.ambiguous), [9, 46],
        )


class SegmentGlueTest(unittest.TestCase):
    """Соседние строки одной позиции (автор, название, год) склеиваются."""

    def test_author_line_before_title(self):
        # Автор подтверждает произведение, но не издание — без года и
        # издательства такое совпадение не уведомляется.
        report = match_text("Дефо\nРобинзон Крузо")
        self.assertEqual([m.book.index for m in report.matches], [38])
        self.assertEqual(report.matches[0].confidence, WEAK)
        self.assertEqual(report.matches[0].evidence, ())
        self.assertTrue(report.confirmed().is_empty)

    def test_author_line_after_title(self):
        report = match_text("Робинзон Крузо\nДефо")
        self.assertEqual([m.book.index for m in report.matches], [38])
        self.assertTrue(report.confirmed().is_empty)

    def test_title_glued_to_author_year_line(self):
        report = match_text("Робинзон Крузо\nДефо. 1929")
        self.assertEqual([m.book.index for m in report.matches], [38])
        self.assertEqual(report.matches[0].confidence, STRONG)
        self.assertTrue(report.matches[0].year_near)

    def test_modern_reprint_description_is_rejected(self):
        # Реальный лот: «Даниэль Дефо - Робинзон Крузо», Эксмо, 2023.
        # Лот об одной книге, поэтому год из любого абзаца — её год издания;
        # 2023 — это переиздание, лот отбрасывается целиком.
        text = (
            "Даниэль Дефо. Роман \"Робинзон Крузо\"\n\n"
            "Москва: Эксмо, 2023. 256 стр. С иллюстрациями.\n\n"
            "Твердый переплёт.\nХорошее состояние."
        )
        report = match_text(text)
        self.assertTrue(report.is_empty)

    def test_single_book_year_in_distant_paragraph(self):
        text = (
            "Робинзон Крузо\n\n"
            "Издательство Academia, Ленинград, 1929 год. Хорошее состояние."
        )
        report = match_text(text)
        self.assertEqual([m.book.index for m in report.matches], [38])
        self.assertEqual(report.matches[0].confidence, STRONG)

    def test_title_glued_to_author_and_year(self):
        report = match_text("Записки о Галльской войне\nЦезарь. 1948")
        self.assertEqual([m.book.index for m in report.matches], [1])
        self.assertEqual(report.matches[0].confidence, STRONG)
        self.assertTrue(report.matches[0].year_near)

    def test_author_title_year_lines(self):
        report = match_text("Цезарь\nЗаписки о Галльской войне\n1948")
        self.assertEqual([m.book.index for m in report.matches], [1])
        self.assertEqual(report.matches[0].confidence, STRONG)

    def test_year_of_neighbour_position_does_not_glue(self):
        # Год 1928 принадлежит «Морфологии сказки» и не должен склеиться
        # с названием «Робинзон Крузо» из соседней строки.
        report = match_text(
            "Книги одним лотом:\n"
            "Робинзон Крузо, новая\n"
            "Пропп. Морфология сказки. 1928"
        )
        by_index = {m.book.index: m for m in report.matches}
        self.assertEqual(by_index[38].evidence, ())
        self.assertEqual(by_index[38].confidence, WEAK)


class AmbiguityTest(unittest.TestCase):
    def test_slovo_bez_goda_daet_dva_varianta(self):
        report = match_text("Слово о полку Игореве. Хорошее состояние")
        self.assertEqual([m.book.index for m in report.matches], [9, 46])
        self.assertTrue(report.needs_year)

    def test_slovo_1950_resolves_to_lp(self):
        self.assertEqual(indexes("Слово о полку Игореве. 1950. Наука"), [9])

    def test_slovo_1934_paleh_resolves_to_academia(self):
        self.assertEqual(indexes("Слово о полку Игореве. 1934. Палех"), [46])

    def test_slovo_1934_without_paleh_resolves_by_year(self):
        self.assertEqual(indexes("Слово о полку Игореве. 1934. Academia"), [46])

    def test_estimate_counts_ambiguous_variants_once(self):
        report = match_text("Слово о полку Игореве")
        low, high = report.estimate()
        self.assertEqual(low, 4500)
        self.assertEqual(high, 35000)


class NegativeMatchTest(unittest.TestCase):
    def test_generic_words_alone_do_not_match(self):
        for text in ("Письма", "Политика", "Опыты", "Земледелие",
                     "Избранные сочинения", "Собрание сочинений",
                     "Книга лирики", "Книга комедий", "Поэтика"):
            with self.subTest(text=text):
                self.assertEqual(indexes(text), [])

    def test_reprints_and_late_editions_are_rejected(self):
        for text in (
            "Пропп. Морфология сказки. 1969",
            "Пропп. Морфология сказки. 2001",
            "Слово о полку Игореве. 1985",
            "Монтень. Опыты. 1979",
            "Манон Леско. 1967",
            "Аристотель. Поэтика. 2007. Академия",
            "Крижанич. Политика. 1985",
            # Новые LP-издания: современные переиздания должны отбрасываться
            "Марк Аврелий. Размышления. 2015. Эксмо",
            "Марк Аврелий. Размышления. 2020. Альпина",
            "Махабхарата. Виратапарва. 2010",
            "Махабхарата. Удьйогапарва. 2005",
            "Цицерон. Письма к Аттику. Т. II. 1990",
            "Цицерон. Письма к Аттику. Т. III. 1985",
        ):
            with self.subTest(text=text):
                self.assertEqual(indexes(text), [])

    def test_reprint_word_is_rejected(self):
        self.assertEqual(indexes("Морфология сказки. Репринт 1928"), [])
        self.assertEqual(indexes("Слово о полку Игореве 1950. Факсимиле"), [])
        self.assertEqual(indexes("Марк Аврелий. Размышления. 1985. Репринт"), [])
        self.assertEqual(indexes("Махабхарата. Виратапарва. Факсимиле"), [])

    def test_cicero_volumes_discriminator_works(self):
        # Номер тома — примета издания: том II (1950) и том III (1951)
        # не смешиваются с томом I (1949), хотя их годы рядом (±1).
        self.assertEqual(indexes("Цицерон. Письма к Аттику. Т. II. 1950"), [71])
        self.assertEqual(indexes("Цицерон. Письма к Аттику. Т. III. 1951"), [72])
        self.assertEqual(indexes("Цицерон. Письма к Аттику. Т. I. 1949"), [4])
        # Том без номера: остаётся только том I — год 1949 в допуске ±1.
        self.assertEqual(indexes("Цицерон. Письма к Аттику. 1950"), [4])

    def test_modern_academia_textbook_is_rejected(self):
        self.assertEqual(indexes("Поэтика. Аристотель. Академия. 2015"), [])

    def test_sbornik_statei_downgraded_to_weak(self):
        match = only("Сборник статей о Старшей Эдде. 1963")
        self.assertEqual(match.confidence, WEAK)

    def test_mladshaya_edda_is_not_starshaya(self):
        self.assertEqual(indexes("Младшая Эдда. 1963"), [])

    def test_bare_mahabharata_without_year_does_not_match(self):
        self.assertEqual(indexes("Махабхарата, хорошее состояние"), [])

    def test_mahabharata_chapters_do_not_cross_match(self):
        # Название главы — единственный ключ: «Махабхарата» в лоте другой
        # главы не должна превращаться в совпадение по Виратапарве/Удьйогапарве.
        self.assertEqual(indexes("Махабхарата. Кн. 1. Адипарва. 1950"), [6])
        self.assertEqual(indexes("Махабхарата. Кн. 4. Виратапарва. 1967"), [73])
        self.assertEqual(indexes("Махабхарата. Кн. 5. Удьйогапарва. 1976"), [74])

    def test_razmyshleniya_requires_mark_aurelius(self):
        # «Размышления» — слишком общее название: без автора совпадения нет.
        self.assertEqual(indexes("Размышления. М.: Питер, 2020"), [])
        self.assertEqual(indexes("Размышления о смысле жизни"), [])
        # Без года и без приметы издания — спорный лот, не подтверждение.
        report = match_text("Марк Аврелий. Размышления. Литературные памятники")
        self.assertTrue(report.confirmed().is_empty)
        self.assertTrue(all(m.needs_year for m in report.matches))

    def test_late_edition_within_few_years_is_rejected(self):
        self.assertEqual(indexes("Пропп. Морфология сказки. 1930"), [])
        self.assertEqual(indexes("Платон. Полное собрание творений. 1925"), [])

    def test_year_within_one_is_tolerated(self):
        # Расхождение в год допускаем (разные тома/годы серии).
        match = only("Цезарь. Записки о Галльской войне. 1949")
        self.assertEqual(match.confidence, STRONG)


class DateFilterTest(unittest.TestCase):
    NOW = datetime(2026, 11, 8, 15, 0, tzinfo=MOSCOW)

    def test_today_with_time(self):
        parsed = parse_posting_date("Сегодня в 14:35", self.NOW)
        self.assertEqual(parsed, datetime(2026, 11, 8, 14, 35, tzinfo=MOSCOW))
        self.assertTrue(is_fresh(parsed, self.NOW))

    def test_today_without_time_is_fresh(self):
        parsed = parse_posting_date("Сегодня", self.NOW)
        self.assertEqual(parsed, self.NOW)

    def test_yesterday_with_time(self):
        parsed = parse_posting_date("Вчера в 09:12", self.NOW)
        self.assertEqual(parsed, datetime(2026, 11, 7, 9, 12, tzinfo=MOSCOW))

    def test_yesterday_without_time_is_stale(self):
        parsed = parse_posting_date("Вчера", self.NOW)
        self.assertEqual(parsed, datetime(2026, 11, 7, 0, 0, tzinfo=MOSCOW))
        self.assertFalse(is_fresh(parsed, self.NOW))

    def test_day_and_month(self):
        parsed = parse_posting_date("8 ноября в 10:00", self.NOW)
        self.assertEqual(parsed, datetime(2026, 11, 8, 10, 0, tzinfo=MOSCOW))

    def test_day_month_and_year(self):
        parsed = parse_posting_date("8 ноября 2025 в 10:00", self.NOW)
        self.assertEqual(parsed, datetime(2025, 11, 8, 10, 0, tzinfo=MOSCOW))

    def test_relative_hours_and_minutes(self):
        self.assertEqual(
            parse_posting_date("2 часа назад", self.NOW),
            self.NOW - timedelta(hours=2),
        )
        self.assertEqual(
            parse_posting_date("5 минут назад", self.NOW),
            self.NOW - timedelta(minutes=5),
        )

    def test_iso_datetime_with_timezone(self):
        self.assertEqual(
            parse_posting_date("2026-11-08T10:00:00+03:00", self.NOW),
            datetime(2026, 11, 8, 10, 0, tzinfo=MOSCOW),
        )
        self.assertEqual(
            parse_posting_date("2026-11-08T10:00:00Z", self.NOW),
            datetime(2026, 11, 8, 13, 0, tzinfo=MOSCOW),
        )

    def test_unknown_date(self):
        self.assertIsNone(parse_posting_date("", self.NOW))
        self.assertIsNone(parse_posting_date("недавно", self.NOW))
        self.assertTrue(is_fresh(None, self.NOW))

    def test_old_explicit_date_is_stale(self):
        parsed = parse_posting_date("1 ноября 2026 в 10:00", self.NOW)
        self.assertFalse(is_fresh(parsed, self.NOW))


class ReviewsFilterTest(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(parse_reviews_count("Нет отзывов"), 0)
        self.assertEqual(parse_reviews_count("12 отзывов"), 12)
        self.assertEqual(parse_reviews_count("1 234 отзыва"), 1234)
        self.assertEqual(parse_reviews_count("1 отзыв"), 1)

    def test_unknown(self):
        self.assertIsNone(parse_reviews_count("Продавец"))
        self.assertFalse(is_wholesale_seller(None))

    def test_wholesale_threshold(self):
        self.assertFalse(is_wholesale_seller(49))
        self.assertTrue(is_wholesale_seller(50))
        self.assertTrue(is_wholesale_seller(120))


if __name__ == "__main__":
    unittest.main()
