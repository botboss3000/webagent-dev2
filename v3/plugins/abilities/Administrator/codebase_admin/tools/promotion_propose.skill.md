# promotion_propose

Creates a durable proposal for the current session's seeded v3 source diff. First run `source_review` and inspect its complete diff, status, and `review_sha256`. Pass that exact digest and a short summary of the change and verification. The tool rechecks the review before saving the proposal. It does not apply anything to the live checkout.

The returned proposal ID identifies a frozen patch. The administrator can inspect it through the authenticated promotions API and approve or reject it separately. If the source changes after the proposal, make a new review and proposal. Do not claim that a proposed patch is deployed.
