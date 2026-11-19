"""Тесты каталога книг: 68 позиций (50 старых + 18 новых)."""

from __future__ import annotations

import unittest

from books.catalog import Book, get_catalog


class CatalogSizeTest(unittest.TestCase):
    def test_catalog_has_68_positions(self):
        catalog = get_catalog()
        assert len(catalog) == 68

    def test_indices_are_contiguous(self):
        indices = [book.index for book in get_catalog()]
        assert indices == list(range(1, 69))

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
    """Новые 18 позиций (51–68): без цен, с диапазонами годов."""

    def setUp(self):
        self.new = [book for book in get_catalog() if book.index >= 51]

    def test_count(self):
        assert len(self.new) == 18

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


if __name__ == "__main__":
    unittest.main()
