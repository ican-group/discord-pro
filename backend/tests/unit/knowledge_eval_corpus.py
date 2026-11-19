"""Synthetic evaluation corpus for Phase 4F knowledge validation (no customer
data). Each answerable case has a gold sentence (copied verbatim from a
document) and hand-written wrong answers labelled with the error category the
Phase 4F instruction asks about. ``quote`` is always an *accurate* quote, so a
wrong answer can only be caught by checking the claim against the evidence."""

DOCUMENTS = {
    "refunds.txt": ("Refund Policy, version 2, effective 1 March 2026. "
                    "Customers may request a refund within 30 days of delivery. "
                    "Refunds over €500 require approval from a finance manager. "
                    "Digital downloads are not eligible for a refund unless the file is defective. "
                    "Refunds are paid to the original payment method within 10 business days."),
    "refunds_2024.txt": ("Refund Policy, version 1, effective 1 January 2024. "
                         "Customers may request a refund within 14 days of delivery. "
                         "Refunds over €200 require approval from a finance manager."),
    "hr_leave.txt": ("Annual leave procedure. Full-time employees receive 25 days of paid annual leave per year. "
                     "Leave requests must be submitted at least 14 days in advance. "
                     "Unused leave of up to 5 days may be carried over to the next year. "
                     "Carried-over leave must be used before 31 March."),
    "support_manual.txt": ("Support manual: passwords and accounts. Reset links expire after 60 minutes. "
                           "Administrators cannot view user passwords. "
                           "Accounts are locked after 5 failed login attempts and unlock automatically after "
                           "15 minutes."),
    "x200_product.txt": ("X200 router product documentation. The X200 router supports up to 64 connected devices. "
                         "Port forwarding requires the Pro plan. The guest network is disabled by default."),
    "contract.txt": ("Master services agreement. Clause 7.2: Either party may terminate the agreement with 90 days "
                     "written notice. Clause 7.3: The supplier may terminate immediately if an invoice remains "
                     "unpaid for more than 60 days. Clause 9.1: Liability is capped at the fees paid in the 12 "
                     "months before the claim, except for fraud or gross negligence."),
    "security_policy.txt": ("Information security policy. Contractors may use company laptops only if approved by "
                            "IT. Personal devices are not permitted on the production network."),
    "shipping.txt": "Shipping policy. Standard shipping is free on orders over €50.",
    "shipping_eu_team.txt": "EU shipping note. Standard shipping is free on orders over €75.",
    "executive_bonus.txt": "Executive compensation. The executive bonus pool is 2 million euro this year.",
    "travel.txt": "Travel policy. Economy class is required for flights under 6 hours.",
}
EXPIRED = {"refunds_2024.txt": "2025-12-31T23:59:59+00:00"}
RESTRICTED = {"executive_bonus.txt"}                    # ACL: another Organisation only

# question, gold doc, gold sentence (verbatim), wrong answers {category: claim}
CASES = [
    ("How many days do customers have to request a refund?", "refunds.txt",
     "Customers may request a refund within 30 days of delivery",
     {"omits_condition": "Customers may request a refund at any time after delivery.",
      "changes_number": "Customers may request a refund within 45 days of delivery.",
      "changes_permission": "Customers must request a refund within 30 days of delivery.",
      "unsupported_conclusion": "Customers may request a refund within 30 days of delivery, so every refund is "
                                "guaranteed."}),
    ("Do large refunds need approval?", "refunds.txt", "Refunds over €500 require approval from a finance manager",
     {"changes_money": "Refunds over €50 require approval from a finance manager.",
      "omits_condition": "Refunds require approval from a finance manager.",
      "changes_permission": "Refunds over €500 may be approved by any employee.",
      "wrong_interpretation": "Only finance managers can request refunds over €500."}),
    ("Can I get a refund for a digital download?", "refunds.txt",
     "Digital downloads are not eligible for a refund unless the file is defective",
     {"changes_exception": "Digital downloads are not eligible for a refund unless the customer changes their mind.",
      "reverses_requirement": "Digital downloads are eligible for a refund.",
      "omits_condition": "Digital downloads are not eligible for a refund.",
      "wrong_interpretation": "Defective digital downloads are not eligible for a refund."}),
    ("How long does a refund payment take?", "refunds.txt",
     "Refunds are paid to the original payment method within 10 business days",
     {"changes_number": "Refunds are paid to the original payment method within 3 business days.",
      "combines_incorrectly": "Refunds are paid to the original payment method within 30 days."}),
    ("How much annual leave do full-time employees get?", "hr_leave.txt",
     "Full-time employees receive 25 days of paid annual leave per year",
     {"changes_number": "Full-time employees receive 30 days of paid annual leave per year.",
      "unsupported_conclusion": "Full-time employees receive 25 days of paid annual leave per year, and part-time "
                                "employees receive the same.",
      "combines_incorrectly": "Full-time employees receive 25 days of paid annual leave, which must be requested "
                              "at least 5 days in advance."}),
    ("When must carried-over leave be used?", "hr_leave.txt", "Carried-over leave must be used before 31 March",
     {"changes_date": "Carried-over leave must be used before 30 June.",
      "reverses_requirement": "Carried-over leave may be used at any time."}),
    ("How far in advance must leave be requested?", "hr_leave.txt",
     "Leave requests must be submitted at least 14 days in advance",
     {"changes_number": "Leave requests must be submitted at least 7 days in advance.",
      "omits_condition": "Leave requests must be submitted in advance."}),
    ("How long are password reset links valid?", "support_manual.txt", "Reset links expire after 60 minutes",
     {"changes_number": "Reset links expire after 24 hours.",
      "unsupported_conclusion": "Reset links expire after 60 minutes, and administrators can extend them."}),
    ("Can administrators see user passwords?", "support_manual.txt", "Administrators cannot view user passwords",
     {"reverses_requirement": "Administrators can view user passwords.",
      "wrong_interpretation": "Administrators cannot reset user passwords."}),
    ("When are accounts locked?", "support_manual.txt",
     "Accounts are locked after 5 failed login attempts and unlock automatically after 15 minutes",
     {"changes_number": "Accounts are locked after 3 failed login attempts and unlock automatically after 15 "
                        "minutes.",
      "omits_condition": "Accounts are locked after failed login attempts."}),
    ("How many devices does the X200 support?", "x200_product.txt",
     "The X200 router supports up to 64 connected devices",
     {"changes_number": "The X200 router supports up to 128 connected devices.",
      "unsupported_conclusion": "The X200 router supports up to 64 connected devices, so it suits enterprises of "
                                "any size."}),
    ("Is port forwarding available?", "x200_product.txt", "Port forwarding requires the Pro plan",
     {"changes_permission": "Port forwarding is available on all plans.",
      "wrong_interpretation": "The Pro plan requires port forwarding."}),
    ("What notice is needed to terminate the agreement?", "contract.txt",
     "Either party may terminate the agreement with 90 days written notice",
     {"changes_number": "Either party may terminate the agreement with 30 days written notice.",
      "changes_permission": "Only the supplier may terminate the agreement with 90 days written notice."}),
    ("Is liability capped?", "contract.txt",
     "Liability is capped at the fees paid in the 12 months before the claim, except for fraud or gross negligence",
     {"changes_exception": "Liability is capped at the fees paid in the 12 months before the claim, except for "
                           "late delivery.",
      "omits_condition": "Liability is capped at the fees paid in the 12 months before the claim."}),
    ("Can contractors use company laptops?", "security_policy.txt",
     "Contractors may use company laptops only if approved by IT",
     {"omits_condition": "Contractors may use company laptops.",
      "reverses_requirement": "Contractors may not use company laptops.",
      "changes_permission": "Contractors must use company laptops."}),
    ("Are personal devices allowed on the production network?", "security_policy.txt",
     "Personal devices are not permitted on the production network",
     {"reverses_requirement": "Personal devices are permitted on the production network.",
      "wrong_interpretation": "Personal devices are permitted on networks other than production."}),
]

# must be refused in strict mode: (question, reason)
REFUSALS = [
    ("What is the parental leave policy?", "no_evidence"),
    ("Does the X200 support Wi-Fi 7?", "no_evidence"),
    ("How big is the executive bonus pool?", "restricted_document"),
    ("Is standard shipping free on all orders?", "contradiction"),
    ("What is the company's revenue this year?", "no_evidence"),
]
