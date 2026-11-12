"""Тесты заголовочного фильтра книжного режима Avito."""

from __future__ import annotations

import unittest

from books.prefilter import lot_title_interesting


class InterestingTitleTest(unittest.TestCase):
    def assert_interesting(self, title: str):
        verdict, reason = lot_title_interesting(title)
        self.assertTrue(verdict, f"ожидали «интересен»: {title}")
        self.assertTrue(reason)

    def assert_boring(self, title: str):
        verdict, _ = lot_title_interesting(title)
        self.assertFalse(verdict, f"ожидали «не интересен»: {title}")

    def test_books_from_list(self):
        for title in (
            "Цезарь. Записки о Галльской войне. 1948. Наука",
            "Слово о полку Игореве. 1950",
            "Пропп. Морфология сказки",
            "Фрейд. Я и Оно",
            "Кабус-намэ",
            "Основания новой науки об общей природе наций",
        ):
            with self.subTest(title=title):
                self.assert_interesting(title)

    def test_authors_from_list(self):
        for title in (
            "Пропп, Монтень, Цицерон — книги одним лотом",
            "Монтень. Опыты, кн. 1",
            "Анри де Ренье. Собрание сочинений",
        ):
            with self.subTest(title=title):
                self.assert_interesting(title)

    def test_series_and_publishers(self):
        for title in (
            "Литературные памятники, 8 книг",
            "Academia. 1930-е. Пять книг",
            "Книги издательства Академия",
            "Антикварные книги XIX века",
            "Старинные книги, 1950 год",
            "Букинистические книги",
        ):
            with self.subTest(title=title):
                self.assert_interesting(title)

    def test_collection_markers(self):
        for title in (
            "Книги из домашней библиотеки, 14 штук",
            "Подборка книг одним лотом",
            "Много книг, всё вместе",
            "Махабхарата, 2 тома",
        ):
            with self.subTest(title=title):
                self.assert_interesting(title)

    def test_unrelated_lots_are_skipped(self):
        for title in (
            "Жириновский. Собрание сочинений",
            "Марафон похудения. Книга-тренинг",
            "Учебник по физике 7 класс",
            "Пылесос Samsung",
            "Книги",
            "Письма",
            "Политика",
        ):
            with self.subTest(title=title):
                self.assert_boring(title)

    def test_late_edition_in_title_is_skipped(self):
        for title in (
            "Слово о полку Игореве. 1985",
            "Пропп. Морфология сказки. 1969",
            "Монтень. Опыты. 1979",
            "Манон Леско. 1967",
        ):
            with self.subTest(title=title):
                self.assert_boring(title)

    def test_empty_title(self):
        self.assert_boring("")
        self.assert_boring("   ")


if __name__ == "__main__":
    unittest.main()
