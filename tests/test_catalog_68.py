"""Тесты каталога книг: 74 позиции (50 старых + 24 новых)."""

from __future__ import annotations

import unittest

from books.catalog import Book, get_catalog


class CatalogSizeTest(unittest.TestCase):
    def test_catalog_has_74_positions(self):
        catalog = get_catalog()
        assert len(catalog) == 74

    def test_indices_are_contiguous(self):
        indices = [book.index for book in get_catalog()]
        assert indices == list(range(1, 75))

    def test_catalog_loads_all_entries(self):
        assert all(isinstance(book, Book) for book in get_catalog())


class OldEntriesTest(unittest.TestCase):
    """Старые 50 позиций остаются без изменений."""

    def setUp(self):
        self.by_index = {book.index: book for book in get_catalog()}

    def _check(self, index, author, title, year, price_min):
        book = self.by_index[index]
        assert book.author == author, (index, book.author)
        assert book.title == title, (index, book.title)
        assert book.year == year, (index, book.year)
        assert book.price_min == price_min, (index, book.price_min)

    def test_spot_checks(self):
        self._check(8, "Летописец (ред.)", "Повесть временных лет, ч. 1", 1950, 4500)
        self._check(9, "—", "Слово о полку Игореве", 1950, 4500)
        self._check(31, "Аристотель", "Поэтика", 1927, 6500)
        self._check(37, "М. Сервантес", "Дон Кихот, т. 1", 1929, 5000)
        self._check(38, "Д. Дефо", "Робинзон Крузо", 1929, 6000)
        self._check(39, "—", "Книга тысячи и одной ночи, т. 1", 1929, 5000)
        self._check(46, "—", "Слово о полку Игореве (Палех, худ. И. Голиков)", 1934, 25000)

    def test_group_of_duplicates_kept(self):
        assert self.by_index[9].group == "слово_о_полку"
        assert self.by_index[46].group == "слово_о_полку"

    def test_needs_author_kept(self):
        book = self.by_index[31]
        assert book.needs_author is True
        assert any("аристотель" in key for key in book.author_keys)

    def test_discriminators_kept(self):
        book = self.by_index[46]
        joined = " ".join(" ".join(key) for key in book.discriminators)
        assert "палех" in joined
        assert "голик" in joined


class NewEntriesTest(unittest.TestCase):
    """Новые 24 позиции (51–74): без цен, с диапазонами годов."""

    def setUp(self):
        self.new = [book for book in get_catalog() if book.index >= 51]

    def test_count(self):
        assert len(self.new) == 24

    def test_no_prices(self):
        for book in self.new:
            assert book.price_min is None, book.index
            assert book.price_max is None, book.index
            assert book.price_top is None, book.index

    def test_year_range_known(self):
        for book in self.new:
            assert book.year_from is not None, book.index
            assert book.year_to is not None, book.index
            assert book.year_from <= book.year_to, book.index

    def test_title_and_keys(self):
        for book in self.new:
            assert book.title, book.index
            assert book.title_keys, book.index

    def test_label_does_not_crash_without_year(self):
        for book in self.new:
            label = book.label
            assert label, book.index

    def test_year_range_property(self):
        by_index = {book.index: book for book in get_catalog()}
        assert by_index[51].year_range == (1922, 1922)
        # Диапазон: журнал «Золотое руно» 1906–1909.
        assert by_index[60].year_range == (1906, 1909)
        assert by_index[60].year is None
        assert "1906" in by_index[60].year_text

    def test_lp_ext_entries_spot_check(self):
        """Позиции 69–74 — издания «Литературных памятников» без цен, серия lp."""
        by_index = {book.index: book for book in get_catalog()}
        # 69: Марк Аврелий, Размышления, 1985, Наука, Ленинград, серия ЛП
        assert by_index[69].year == 1985
        assert by_index[69].year_range == (1985, 1985)
        assert by_index[69].series == "lp"
        assert by_index[69].title == "Размышления"
        assert "марк" in " ".join(" ".join(k) for k in by_index[69].author_keys)
        assert "аврелий" in " ".join(" ".join(k) for k in by_index[69].author_keys)
        # 70: Марк Аврелий, Размышления, 1993, Наука, СПб., 2-е изд., серия ЛП
        assert by_index[70].year == 1993
        assert by_index[70].series == "lp"
        # 71: Цицерон, Письма... Т. II, 1950, серия ЛП
        assert by_index[71].year == 1950
        assert by_index[71].series == "lp"
        assert by_index[71].author == "Марк Туллий Цицерон"
        # 72: Цицерон, Письма... Т. III, 1951, серия ЛП
        assert by_index[72].year == 1951
        assert by_index[72].series == "lp"
        # 73: Махабхарата. Кн. 4. Виратапарва, 1967, серия ЛП
        assert by_index[73].year == 1967
        assert by_index[73].series == "lp"
        assert "виратапарва" in " ".join(" ".join(k) for k in by_index[73].title_keys)
        # 74: Махабхарата. Кн. 5. Удьйогапарва, 1976, серия ЛП
        assert by_index[74].year == 1976
        assert by_index[74].series == "lp"
        assert "удьйогапарва" in " ".join(" ".join(k) for k in by_index[74].title_keys)


if __name__ == "__main__":
    unittest.main()
