from datetime import date
import unittest

from apm_notifier.classify import ACCEPT, REJECT, REVIEW, classify, detect_term, evaluate_graduation
from apm_notifier.models import Job


TODAY = date(2026, 11, 12)


def posting(title: str, location: str, description: str, **extra) -> Job:
    return Job("example", "Example Co", title, "https://example.com/jobs/1", location, description=description, **extra)


PM_DUTIES = """
What you'll do:
- Own a slice of the product roadmap and help prioritize the backlog with your squad.
- Write product requirements documents (PRDs) and user stories for new features.
- Run customer interviews during discovery and partner with engineering and design.
- Define success metrics and analyze product analytics after launch.
"""


class RequiredClassifierCases(unittest.TestCase):
    def test_mba_only_role_is_rejected(self) -> None:
        job = posting(
            "MBA Product Manager Intern - Summer 2027",
            "Seattle, WA",
            PM_DUTIES
            + "Qualifications:\n- Currently enrolled in a full-time MBA program, graduating in 2028.\n"
            "- Visa sponsorship is available for this role.",
        )
        result = classify(job, TODAY)
        self.assertEqual(result.decision, REJECT)
        self.assertIn("MBA", result.reason)

    def test_mba_only_description_without_mba_title_is_rejected(self) -> None:
        job = posting(
            "Product Manager Intern, Summer 2027",
            "Toronto, ON",
            PM_DUTIES + "Minimum qualifications:\n- Currently pursuing an MBA or Master's degree.",
        )
        result = classify(job, TODAY)
        self.assertEqual(result.decision, REJECT)
        self.assertIn("pursuing an MBA", " ".join(result.quotes))

    def test_graduating_2026_or_2027_is_rejected(self) -> None:
        job = posting(
            "Associate Product Manager Intern (Summer 2027)",
            "Toronto, Ontario, Canada",
            PM_DUTIES + "Who you are:\n- Expected graduation between December 2026 and June 2027.",
        )
        result = classify(job, TODAY)
        self.assertEqual(result.decision, REJECT)
        self.assertTrue(result.reason.startswith("Graduation year"))
        self.assertIn("December 2026 and June 2027", " ".join(result.quotes))

    def test_us_onsite_without_sponsorship_is_rejected(self) -> None:
        job = posting(
            "Product Manager Intern - Summer 2027",
            "San Francisco, CA",
            PM_DUTIES
            + "Requirements:\n- Pursuing a Bachelor's degree, graduating in 2028.\n"
            "- This role is not eligible for immigration sponsorship.",
        )
        result = classify(job, TODAY)
        self.assertEqual(result.decision, REJECT)
        self.assertIn("not eligible for immigration sponsorship", " ".join(result.quotes))

    def test_canadian_pm_intern_without_grad_year_is_accepted(self) -> None:
        job = posting(
            "Product Management Intern",
            "Vancouver, British Columbia, Canada",
            PM_DUTIES
            + "This is a 4-month internship starting May 2027.\n"
            "- Currently enrolled in a Bachelor's program. Must be legally eligible to work in Canada.",
        )
        result = classify(job, TODAY)
        self.assertEqual(result.decision, ACCEPT, result.reasons)
        self.assertEqual(result.category, "core")
        self.assertEqual(result.term_length, "4 months")
        self.assertIn("eligible to work in Canada", " ".join(result.quotes))

    def test_us_remote_product_analyst_intern_is_accepted(self) -> None:
        job = posting(
            "Product Analyst Intern, Summer 2027",
            "Remote - United States",
            "You will analyze product metrics, run A/B tests and partner with product managers.\n"
            "We are looking for undergraduate students returning to school after the internship.",
        )
        result = classify(job, TODAY)
        self.assertEqual(result.decision, ACCEPT, result.reasons)
        self.assertEqual(result.category, "adjacent")
        self.assertEqual(result.remote_status, "remote")

    def test_us_onsite_silent_on_sponsorship_needs_review(self) -> None:
        job = posting(
            "Product Manager Intern (Summer 2027)",
            "New York, NY",
            PM_DUTIES + "Pursuing a bachelor's degree in any discipline.",
        )
        result = classify(job, TODAY)
        self.assertEqual(result.decision, REVIEW)
        self.assertIn("sponsorship not mentioned", result.reason)

    def test_full_time_new_grad_pm_is_rejected(self) -> None:
        job = posting(
            "Associate Product Manager, New Grad 2027 (Full-time)",
            "Toronto, ON",
            PM_DUTIES + "Graduated or graduating with a bachelor's degree by June 2027.",
        )
        result = classify(job, TODAY)
        self.assertEqual(result.decision, REJECT)
        self.assertIn("new-grad", result.reason)

    def test_toronto_program_manager_coop_is_accepted_with_gta_priority(self) -> None:
        job = posting(
            "Program Manager Co-op (Summer 2027, 8 months)",
            "Toronto, Ontario",
            "Coordinate cross-functional delivery of our mobile app releases and track milestones.\n"
            "Open to students returning to full-time studies after the work term.",
        )
        result = classify(job, TODAY)
        self.assertEqual(result.decision, ACCEPT, result.reasons)
        self.assertTrue(result.priority_location)
        self.assertEqual(result.term_length, "8 months")


class AdditionalClassifierCases(unittest.TestCase):
    def test_pure_software_engineering_intern_is_rejected(self) -> None:
        result = classify(posting("Software Engineer Intern - Summer 2027", "Toronto, ON", "Build services in Go."), TODAY)
        self.assertEqual(result.decision, REJECT)
        self.assertIn("Not a product role", result.reason)

    def test_generic_intern_title_judged_on_description(self) -> None:
        product = classify(posting("2027 Summer Internship Program - Customer Platforms", "Toronto, ON", PM_DUTIES), TODAY)
        self.assertEqual(product.decision, ACCEPT, product.reasons)
        self.assertEqual(product.category, "adjacent")
        non_product = classify(posting("2027 Summer Internship - Treasury", "Toronto, ON", "Reconcile cash positions and prepare journal entries." * 10), TODAY)
        self.assertEqual(non_product.decision, REJECT)

    def test_generic_title_without_description_is_an_audited_reject(self) -> None:
        result = classify(posting("Compiler Engineering Intern", "Cupertino, CA", ""), TODAY)
        self.assertEqual(result.decision, REJECT)
        conditional = classify(posting("Business Analyst Intern", "Toronto, ON", ""), TODAY)
        self.assertEqual(conditional.decision, REVIEW)

    def test_boilerplate_teamwork_words_are_not_product_work(self) -> None:
        text = "Work cross-functionally, prioritize tasks, track KPIs and launch new products with the finance team. " * 3
        self.assertEqual(classify(posting("Summer Intern 2027 - Finance Operations", "Toronto, ON", text), TODAY).decision, REJECT)

    def test_fall_2027_and_summer_2026_terms_are_rejected(self) -> None:
        fall = classify(posting("Product Manager Co-op - Fall 2027", "Toronto, ON", PM_DUTIES), TODAY)
        self.assertEqual(fall.decision, REJECT)
        self.assertIn("Term outside", fall.reason)
        old = classify(posting("Product Manager Intern", "Toronto, ON", PM_DUTIES + "The internship runs May - August 2026."), TODAY)
        self.assertEqual(old.decision, REJECT)

    def test_canadian_twelve_month_term_starting_may_2027_is_accepted(self) -> None:
        result = classify(
            posting("Product Owner Intern (12 months)", "Mississauga, ON", PM_DUTIES + "Term: May 2027 to April 2028."),
            TODAY,
        )
        self.assertEqual(result.decision, ACCEPT, result.reasons)
        self.assertEqual(result.term_length, "12 months")

    def test_uk_role_is_rejected_and_part_time_school_year_is_rejected(self) -> None:
        uk = classify(posting("Product Manager Intern - Summer 2027", "London, United Kingdom", PM_DUTIES), TODAY)
        self.assertEqual(uk.decision, REJECT)
        self.assertIn("outside Canada/US", uk.reason)
        part_time = classify(
            posting("Product Operations Intern", "Toronto, ON", PM_DUTIES + "This is a part-time role during the school year (15 hours/week)."),
            TODAY,
        )
        self.assertEqual(part_time.decision, REJECT)

    def test_citizenship_requirement_and_cpt_support(self) -> None:
        citizen = classify(posting("Product Manager Intern 2027", "Austin, TX", PM_DUTIES + "Must be a U.S. citizen."), TODAY)
        self.assertEqual(citizen.decision, REJECT)
        cpt = classify(posting("Product Manager Intern 2027", "Austin, TX", PM_DUTIES + "International students on CPT are welcome to apply."), TODAY)
        self.assertEqual(cpt.decision, ACCEPT, cpt.reasons)

    def test_expired_deadline_is_rejected(self) -> None:
        result = classify(
            posting("Product Manager Intern, Summer 2027", "Toronto, ON", PM_DUTIES + "Application deadline: October 2, 2026."),
            TODAY,
        )
        self.assertEqual(result.decision, REJECT)
        self.assertEqual(result.status, "closed")

    def test_graduate_degree_requirement_is_not_rescued_by_unrelated_undergrad_text(self) -> None:
        job = posting(
            "2027 Summer Internship Graduate Leadership Program - Digital Banking",
            "Toronto, ON",
            PM_DUTIES + "We partner with undergraduate clubs across Canada for events.\n" + ("Our team culture values curiosity. " * 8)
            + "\nRequirements:\n- Pursuing a Graduate Degree in a related field.",
        )
        result = classify(job, TODAY)
        self.assertEqual(result.decision, REJECT)
        self.assertIn("Graduate Degree", " ".join(result.quotes))

    def test_bachelors_or_masters_requirement_accepts(self) -> None:
        job = posting("Product Manager Intern, Summer 2027", "Toronto, ON", PM_DUTIES + "Currently pursuing a Bachelor's or Master's degree.")
        self.assertEqual(classify(job, TODAY).decision, ACCEPT)

    def test_penultimate_or_final_year_language_accepts(self) -> None:
        verdict, _, _ = evaluate_graduation("Open to students in their penultimate or final year of study.", 2028, 4)
        self.assertEqual(verdict, "ok")

    def test_graduation_window_including_april_2028_accepts(self) -> None:
        verdict, _, quoted = evaluate_graduation("Expected graduation between December 2027 and June 2028.", 2028, 4)
        self.assertEqual(verdict, "ok")
        self.assertIn("December 2027", quoted)

    def test_graduation_dates_are_not_mistaken_for_the_internship_term(self) -> None:
        text = PM_DUTIES + "Pursuing a bachelor's degree with expected graduation between December 2027 - May 2029, or in spring/summer of 2028."
        result = classify(posting("Product Management Intern", "Toronto, ON", text), TODAY)
        self.assertEqual(result.decision, ACCEPT, result.reasons)

    def test_past_start_date_in_description_needs_review_not_reject(self) -> None:
        result = classify(posting("Product Manager Intern", "Toronto, ON", PM_DUTIES + "Applications open with interviews starting August 2026."), TODAY)
        self.assertEqual(result.decision, REVIEW)

    def test_spring_2027_is_ambiguous(self) -> None:
        self.assertEqual(detect_term("Product Intern - Spring 2027", "", "", 2027).verdict, "review")
        self.assertEqual(detect_term("Product Intern - Winter/Summer 2027", "", "", 2027).verdict, "ok")


if __name__ == "__main__":
    unittest.main()


class SponsorshipDenialPhrasings(unittest.TestCase):
    BASE = PM_DUTIES + "Requirements:\n- Pursuing a Bachelor's degree, graduating in 2028.\n- "

    def test_common_denials_are_rejected(self) -> None:
        for phrase in (
            "We will not sponsor applicants for work visas.",
            "Visa sponsorship is not available for this position.",
            "Applicants who now or in the future require sponsorship will not be considered.",
            "Candidates requiring visa sponsorship are not eligible for this role.",
            "We do not provide H-1B sponsorship.",
        ):
            with self.subTest(phrase=phrase):
                result = classify(posting("Product Manager Intern 2027", "New York, NY", self.BASE + phrase), TODAY)
                self.assertEqual(result.decision, REJECT)

    def test_sponsorship_offer_is_not_mistaken_for_denial(self) -> None:
        result = classify(posting("Product Manager Intern 2027", "New York, NY", self.BASE + "We will provide visa sponsorship."), TODAY)
        self.assertNotEqual(result.decision, REJECT)

    def test_denial_with_unstated_location_is_rejected(self) -> None:
        result = classify(posting("Product Manager Intern 2027", "", self.BASE + "Must be a U.S. citizen."), TODAY)
        self.assertEqual(result.decision, REJECT)


class RequestedCompanyTitles(unittest.TestCase):
    def test_teck_business_analyst_coop_is_accepted_without_product_signals(self) -> None:
        job = Job(
            "teck", "Teck Resources Limited", "Business Analyst Co-op", "https://jobs.teck.com/job/1",
            "Vancouver, BC, CA",
            description="Join our Vancouver office for an 8-month co-op work term. You will document business "
            "processes, gather requirements from stakeholders and support reporting for mining operations. "
            "Open to students currently enrolled in a post-secondary program returning to school after the term.",
        )
        result = classify(job, TODAY)
        self.assertNotEqual(result.decision, REJECT, result.reason)
