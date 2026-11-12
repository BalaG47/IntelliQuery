import unittest

from query_router import ArgoQueryRouter, Route


class ArgoQueryRouterTests(unittest.TestCase):
    def test_profile_count_variants_use_mysql(self):
        questions = [
            "How many profiles are available for each float?",
            "Show profile counts by WMO number",
            "number of profiles per float id",
        ]
        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(
                    ArgoQueryRouter.route(question).route,
                    Route.MYSQL_PROFILE_COUNTS,
                )

    def test_float_enumeration_variants_use_mysql(self):
        questions = [
            "List all float IDs",
            "List available floats",
            "What floats are available?",
            "Which WMO numbers are available?",
            "How many floats do we have?",
        ]
        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(
                    ArgoQueryRouter.route(question).route,
                    Route.MYSQL_FLOAT_ENUMERATION,
                )

    def test_wmo_number_always_uses_mysql(self):
        decision = ArgoQueryRouter.route("Show temperature for WMO 5901234")
        self.assertEqual(decision.route, Route.MYSQL_WMO)
        self.assertEqual(decision.wmo_number, 5901234)

    def test_count_for_one_wmo_uses_an_exact_mysql_count(self):
        decision = ArgoQueryRouter.route("floatID WMO 2900228 contains how many profiles")
        self.assertEqual(decision.route, Route.MYSQL_WMO_PROFILE_COUNT)
        self.assertEqual(decision.wmo_number, 2900228)

    def test_arabian_sea_year_range_trend_uses_mysql(self):
        self.assertEqual(
            ArgoQueryRouter.route(
                "whats the salinity trend throughout the years 2010 - 2016 in arabian sea"
            ).route,
            Route.MYSQL_REGIONAL_TREND,
        )

    def test_semantic_question_uses_chromadb(self):
        self.assertEqual(
            ArgoQueryRouter.route("Find profiles with cold deep water").route,
            Route.CHROMA_SEMANTIC,
        )

    def test_general_conversation_does_not_use_chromadb(self):
        messages = [
            "hi",
            "hello",
            "hey",
            "good morning",
            "how are you",
            "thanks",
            "thank you",
            "bye",
        ]

        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(
                    ArgoQueryRouter.route(message).route,
                    Route.GENERAL_CONVERSATION,
                )


if __name__ == "__main__":
    unittest.main()
