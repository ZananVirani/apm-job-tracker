import unittest

from apm_notifier.filtering import RoleFilter, location_evidence, title_category


class TitleGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.filter = RoleFilter(2027)

    def test_keeps_internships_and_product_family_titles(self) -> None:
        for title in (
            "Product Manager Intern - Summer 2027",
            "APM Intern",
            "AI Product Manager Intern",
            "Technical Program Manager Co-op (Fall/Summer)",
            "2027 Summer Internship Program - Customer Platforms",
            "Software Engineer Intern",  # stored as an audited reject
            "Student, Junior Financial Analyst (Winter 2027)",
            "Associate Product Manager",  # full-time PM roles are kept for audit
            "Global Markets Summer Analyst 2027",
            "Stagiaire en gestion de produits",
        ):
            with self.subTest(title=title):
                self.assertTrue(self.filter.matches(title))

    def test_drops_senior_unrelated_and_past_cohort_titles(self) -> None:
        for title in (
            "Senior Product Manager",
            "Director, Product Management",
            "Product Manager II",
            "Account Executive",
            "Product Manager Intern - Summer 2026",
            "Data Engineer",
            "Senior Manager Strategic Planning - Student Banking Lead",
            "noogler_hat noogler_hat Students Students",
        ):
            with self.subTest(title=title):
                self.assertFalse(self.filter.matches(title))

    def test_title_categories(self) -> None:
        cases = {
            "Product Manager Intern": "core",
            "Associate Product Manager (APM) Intern": "core",
            "Product Analyst Intern": "adjacent",
            "Program Manager Co-op": "adjacent",
            "Digital Strategy Intern": "adjacent",
            "Business Analyst Intern": "adjacent",
            "Forward Deployed Engineer Intern": "adjacent",
            "Product Designer Intern": "excluded",
            "Software Developer Co-op": "excluded",
            "Data Scientist Intern": "excluded",
            "Sales Development Intern": "excluded",
            "Internal Audit Co-op": "excluded",
            "Summer Intern 2027": "other",
            "Backend Software Engineer Intern (TikTok-Growth-Innovative Growth)": "excluded",
            "Machine Learning Engineer Intern (E-Commerce User Growth)": "excluded",
            "Design System Design Intern (Monetization Ads)": "excluded",
            "Business Controller Intern": "excluded",
            "Growth Marketing Intern": "adjacent",
            "Software Engineer Intern - Technology Healthcare Innovation": "excluded",
            "Innovation & Technology Data Science Intern": "excluded",
            "Summer 2027 Intern - Venture Capital": "adjacent",
            "2027 Summer Internship (Digital Product)": "adjacent",
            "Technology Intern": "other",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(title_category(title).category, expected)
        self.assertTrue(title_category("Business Analyst Intern").needs_product_description)
        self.assertFalse(title_category("Product Analyst Intern").needs_product_description)
        self.assertTrue(title_category("Environmental Innovation Intern").needs_product_description)


class LocationEvidenceTests(unittest.TestCase):
    def test_canada_and_gta(self) -> None:
        evidence = location_evidence("Toronto, ON, CA")
        self.assertEqual(evidence.countries, {"CA"})
        self.assertTrue(evidence.gta)
        self.assertFalse(location_evidence("Waterloo, Ontario").gta)
        self.assertTrue(location_evidence("Waterloo, Ontario").waterloo)
        self.assertEqual(location_evidence("Mississauga").countries, {"CA"})

    def test_united_states_and_california_abbreviation(self) -> None:
        self.assertEqual(location_evidence("San Jose, CA").countries, {"US"})
        self.assertEqual(location_evidence("Plano, TX").countries, {"US"})
        for shorthand in ("SF", "LA", "NYC", "Silicon Valley, CA", "Remote in USA"):
            self.assertEqual(location_evidence(shorthand).countries, {"US"}, shorthand)
        self.assertEqual(location_evidence("Toronto, ON; Austin, TX").countries, {"CA", "US"})

    def test_remote_generic_and_foreign(self) -> None:
        remote = location_evidence("Remote")
        self.assertTrue(remote.remote and remote.generic and not remote.countries)
        self.assertFalse(remote.foreign_only)
        self.assertTrue(location_evidence("London, United Kingdom").foreign_only)
        self.assertTrue(location_evidence("", country_codes=("SG",)).foreign_only)
        self.assertEqual(location_evidence("", country_codes=("CA",)).countries, {"CA"})
        self.assertTrue(location_evidence("2 Locations").generic)


if __name__ == "__main__":
    unittest.main()
