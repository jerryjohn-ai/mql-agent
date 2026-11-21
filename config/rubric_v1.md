# ICP rubric v1 (demo: GenWatt generators, Trailhead sample org)

Score each lead from 0 to 100 by adding the four parts below. Use only facts
returned by tools or present in the lead form. If an input is unknown, score
that part conservatively and list the input in `missing_fields`.

## 1. Firmographic fit (0-40)
- Employees: 5,000+ = 20; 1,000-4,999 = 15; 200-999 = 8; under 200 = 2; unknown = 0
- Industry: Energy, Utilities, Manufacturing, Construction, Transportation,
  Healthcare, Hospitality = 15; other named industry = 5; unknown = 0
- Annual revenue: 100M+ = 5; 10M-100M = 3; under 10M or unknown = 0

## 2. Buyer role (0-25)
- C-level or SVP = 25; VP = 20; Director = 15; Manager = 8; other or unknown = 0
- Facilities, Operations, Production, Procurement and Technology roles are the
  primary buyers; Finance roles are secondary (subtract 5, minimum 0).

## 3. Intent (0-25)
- Free text names a concrete need (backup power, outages, sites, timeline) = 25
- Free text present but vague = 10
- No free text: Web or Phone Inquiry = 10; Partner Referral = 12; Purchased List = 0

## 4. Relationship (0-10)
- Existing customer account found = 10
- Prior leads from this email that converted = 5
- Prior leads from this email disqualified = 0 and note it in the rationale

## Recommended action
- route_to_sdr when the total is 80 or more
- disqualify when the total is under 50 and the data is reasonably complete
- otherwise nurture

Confidence reflects how complete and consistent the evidence is, not how good
the lead is. Lower it when key inputs are unknown or sources disagree.
