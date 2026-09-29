# 30-Day Outbound Playbook: Avnish Rana

**Goal by Oct 30, 2026:** 4-5 freelance contracts at $10-12/hr (₹800-1,000/hr), **or** 2-3 product/AI-engineering internship offers.
**Engine:** 38 new, researched, personalised prospects per day, each on a 4-email threaded sequence. Your manual work is about 20 minutes a day reviewing drafts, plus answering replies.

---

## 0. An honest read of where you stand

**Stronger than you think:**
- You have a **production system with real users and real numbers**: the real-estate CRM, WhatsApp bot, ad attribution and LLM lead classification. Most freelancers pitching "AI automation" have demos. You have a live system a sales team uses every day. Every email should lead with this.
- Your resume shows **commercial thinking** (ROI models used in live sales, funnel work, PRDs). Owners pay for that, not for code.
- A current title, **AI Researcher at Caudal AI**, which answers "are you any good?" without you having to say more (NDA-safe).

**What makes you read as a beginner, and the fix:**

| Reads as junior | Reads as experienced |
|---|---|
| "Intern at X, Y, Z" as the headline | "I built and run the CRM + WhatsApp automation a Delhi-NCR brokerage's sales team uses daily" |
| Listing technologies | One outcome with a number: "recovered 722 of 746 blank leads (97%)" |
| "I'd love the opportunity to learn" | "Here's what I'd build for you in 2 weeks, fixed price" |
| Asking for a call in email 1 | Offering a free 1-page plan (lower friction, and it shows expertise) |
| Hourly rate first | Fixed-scope milestones that work out to $10-12/hr |

**Rules you keep:** never claim years of experience or clients you don't have. Specific, verifiable outcomes do the work. The drafting prompt enforces this (`config/profile.yaml` is the only source of facts it may use).

**Do this on day 1:** ask the brokerage owner (Puneet) for (a) permission to name the brokerage, (b) a 2-line testimonial, (c) one referral to another broker or builder they know. A warm intro from a paying client is worth more than 200 cold emails. Until you have permission, the copy says "a Delhi-NCR real-estate brokerage".

---

## 1. Niche choice: where 38 emails a day turn into offers

The winning niche is **the one where your proof matches the buyer's pain exactly**, the buyer can say yes without a committee, and few competitors pitch them.

| Segment | Daily | Why it fits you | Competition | Deal shape |
|---|---|---|---|---|
| **Real-estate brokerages & channel partners, India** (NCR, Mumbai, Pune, Bengaluru, Hyderabad) | 12 | Your case study *is* their business: 99acres/MagicBricks/FB leads, WhatsApp, agents, site visits | Low for custom builders. They get pitched SaaS CRMs, not someone who wires their actual workflow | ₹25-45k 2-week build + ₹8-15k/month upkeep |
| **Real-estate brokerages, UAE** (Dubai, Sharjah, Abu Dhabi; many Indian-run) | 6 | Same workflow on Bayut/Property Finder/Dubizzle; WhatsApp-first market; higher budgets; IST≈GST | Low-medium | AED 1,500-3,000 builds, $10-15/hr |
| **Digital/web agencies, India** (5-50 people, selling to SMBs) | 4 | Their clients ask for WhatsApp bots, CRM and AI features; you become their white-label dev | Medium | Repeat hourly work; one agency can supply 2-3 contracts |
| **Small agencies, UK/Ireland** | 4 | Local devs cost £50-90/hr; $10-12/hr with UK overlap is an easy yes for small tasks | Medium, but most offshore pitches are generic | Hourly, recurring |
| **Seed-Series A AI/SaaS startups, India** (internships) | 12 | Founders want people who ship end to end; you have production + product proof | High volume of applicants, but almost none email founders with a product idea | Internship offer |

**Why not general SMBs, D2C or clinics:** your proof doesn't mirror their pain, so you'd be one more "AI automation" pitch. Stay narrow for 30 days. Narrow looks experienced.

**Rebalance at day 14** using `python -m outreach report`: move the daily quota from the segment with the lowest positive-reply rate to the highest. Change `daily_new` in `config/settings.yaml`.

---

## 2. Offers (what the emails actually sell)

Buyers don't buy hours; they buy an outcome with a date. Price by project and make sure it works out to $10-12/hr (keep that math to yourself).

1. **Lead Leak Fix (real estate)**, 2 weeks: every portal/ad/website/WhatsApp lead goes into one CRM (theirs, or a lightweight one you set up), gets an instant WhatsApp reply, is auto-assigned to an agent with a call-within-30-minutes reminder, and gets a source tag so the owner sees which portal or ad produces deals. **₹25-45k** or **AED 1,500-3,000**. Then upkeep at ₹8-15k/month.
2. **Paid pilot (anyone hesitant):** one fixed-price milestone, ₹8-15k / $150-250, delivered in 5 days. It de-risks the first "yes" and nearly always turns into the full project.
3. **White-label automation dev (agencies):** $10-12/hr or fixed per task. WhatsApp Cloud API, CRM/lead routing, n8n/Zapier, Meta Lead Ads sync, LLM features. First task at a fixed price.
4. **Free 1-page plan (the CTA):** sent within 24 hours of a yes. It shows expertise at no cost to them and leads naturally to the proposal.

**Delivery promise (your differentiator, and you must keep it):** written scope within 24 hours, working demo in week 1, a daily async update, on-time or a discount you state up front.

---

## 3. Funnel math: what it takes to hit the target

At 38 new prospects a day on Mon-Fri (plus Saturdays for Indian brokers), you'll contact about **750-800 people by day 30**, split 26 freelance and 12 internship per day. Freelance sending starts on day 8, so it has about 3 weeks.

Rates below are realistic for **researched, plain-text, 4-touch** sequences to a narrow niche. Generic blasts get about a third of this.

| Freelance (≈450 contacted) | Conservative | Good |
|---|---|---|
| Reply rate (after 4 touches) | 6% → 27 | 10% → 45 |
| Positive (want the plan / call) | 3% → 13 | 5% → 22 |
| Calls held | 8 | 14 |
| Closed (pilot or project) | 2-3 | 4-6 |

| Internships (≈300 contacted) | Conservative | Good |
|---|---|---|
| Reply rate | 8% → 24 | 14% → 42 |
| Interviews | 8 | 15 |
| Offers | 1-2 | 3-4 |

**The target is reachable, but only in the "good" column.** Four levers move you there, and they matter more than volume:
1. **Reply to positive replies within 1 hour** (the tool drafts the reply and pings your phone).
2. **Deliver the 1-page plan within 24 hours.** It closes more deals than any call.
3. **Warm channel on the side:** the referral from your current client, plus a LinkedIn connection request (no pitch) to the same person on the day email 1 goes out, for the top 5 leads each day. Do this by hand; automating LinkedIn gets accounts banned.
4. **For the top 10 startups:** attach real work. A 1-page teardown of their onboarding or a small prototype turns a cold email into an interview.

Checkpoints: by **day 14**, ≥4% reply rate and ≥2 calls booked. By **day 21**, ≥1 pilot signed or ≥3 interviews. If you're behind, see section 9.

---

## 4. Infrastructure checklist

### 4.1 Domains (day 1), ~₹1,800 total
- [ ] **Never cold-email from your main portfolio domain.** Keep `avnishrana.<tld>` (or a GitHub Pages site) as the portfolio.
- [ ] Buy **2 sending domains** that look like you, e.g. `avnishrana.co`, `getavnish.com`, `avnishbuilds.com`, `ranaautomation.com`. Use Cloudflare Registrar (at-cost) or Porkbun. Pick .com/.co/.in over cheap TLDs (.xyz, .top), which are spam-filtered.
- [ ] Redirect both sending domains' web root to your portfolio (Cloudflare → Rules → Redirect). A prospect who checks the domain should land on your case study.

### 4.2 Inboxes (day 1), ~₹400-500/month
- [ ] **Zoho Mail Lite**, 2 users per domain = 4 inboxes (about ₹59-90/user/month + GST; IMAP/SMTP included, which the pipeline needs). The Zoho *free* plan has no IMAP/SMTP, so it won't work. Google Workspace (~$7/user) also works if you prefer it.
- [ ] Names: `avnish@domain1`, `rana@domain1`, `avnish@domain2`, `hello@domain2`. Display name "Avnish Rana" on all of them, plus the same profile photo.
- [ ] **Personal Gmail** (`avnishrana797@gmail.com`, years old = good reputation) sends **internship emails only**, capped at 20/day. Founders are used to personal emails from candidates.
- [ ] Create app passwords (Gmail: 2-step verification → App passwords; Zoho: Security → App passwords) and put them in `.env`.

### 4.3 DNS for each sending domain (day 1, 30 minutes)
- [ ] **MX** records from Zoho's setup wizard.
- [ ] **SPF** (one TXT record only): `v=spf1 include:zoho.in ~all` (`zoho.com` for a non-India data centre; Zoho's wizard shows the exact value).
- [ ] **DKIM**: generate it in the Zoho admin panel (Domains → Email configuration → DKIM), add the TXT record, click Verify.
- [ ] **DMARC**: TXT at `_dmarc`: `v=DMARC1; p=none; rua=mailto:hello@domain2`. Move to `p=quarantine` after 2 clean weeks.
- [ ] **No open or click tracking.** Tracking pixels and rewritten links hurt deliverability, and Apple Mail makes open rates meaningless anyway. We measure replies only (the pipeline doesn't track).
- [ ] Check each inbox: send to the address shown on **mail-tester.com** (aim for ≥9/10). Check the domain on **MXToolbox** (SPF/DKIM/DMARC/blacklists). Sign up for **Google Postmaster Tools** for both domains.

### 4.4 Warm-up (day 1 onward; keep it running all month)
New domains have no reputation. Blasting from them on day 1 lands you in spam and burns the domain.
- [ ] Turn on a warm-up network for the 4 domain inboxes. Truly free options are rare now: **TrulyInbox** has a free tier (1 inbox, 10/day). Warmup Inbox, Mailivery and Warmy offer ~7-day trials. Instantly bundles warm-up into its paid plan. Recommended: the free tier on one inbox, stagger trials across the others, and add the manual warm-up below.
- [ ] Manual warm-up, days 1-7: have 10-15 friends/classmates on Gmail and Outlook email each inbox and get real replies back. Subscribe to 5 newsletters per inbox. Move anything that lands in spam to the inbox.
- [ ] The ramp is automatic in `settings.yaml → ramp_by_week: [0, 12, 25, 35]`: week 0 no cold sends, week 1 up to 12/inbox/day, week 2 25, week 3+ 35. Follow-ups count toward the cap.
- **The trade-off:** standard advice is 2-3 weeks of warm-up. You have 30 days, so cold sending starts at low volume after 1 week. If mail-tester drops below 8 or Postmaster shows reputation "Low/Bad", hold the ramp (edit `ramp_by_week`) until it recovers.

### 4.5 Capacity (why 5 inboxes)
38 new/day is the *new* prospects. Each gets up to 3 follow-ups, so by week 3 you're sending **~150 emails/day in total**. Safe per-inbox volume is ≤35/day, so: freelance runs on 3 domain inboxes (26 new + follow-ups ≈ 100/day), and internship on Gmail (20) + 1 domain inbox (35).

### 4.6 Trust assets (days 1-3); prospects *will* check you
- [ ] **One-page portfolio** (GitHub Pages/Vercel, free): headline "I build WhatsApp, CRM & AI automations that stop businesses losing leads", then the brokerage case study (problem → what you built → numbers → screenshot with client data blurred), then 2-3 other proof points, then contact and booking link.
- [ ] **2-minute Loom demo** of the CRM flow with dummy data: lead arrives → WhatsApp auto-reply → agent assigned → calendar reminder. Link it only in follow-ups or replies, never in email 1.
- [ ] **Cal.com** free booking link (20-minute slots, IST and GST/UK hours).
- [ ] LinkedIn headline: "AI Researcher @ Caudal AI | Building WhatsApp, CRM & AI automations | ex-Klimashift". Pin the case study as a featured post.
- [ ] Payment setup: Razorpay payment links/UPI for India; Wise or Payoneer for USD/AED/GBP. Keep a simple 1-page contract and invoice template ready.

**Budget for the month:** domains ~₹1,800 + Zoho ~₹500 + Claude API ~₹4,000-5,000 (about $2/day on the default model, roughly half with `OUTREACH_MODEL=claude-sonnet-5-5`) + warm-up trials ₹0-1,000. **About ₹7-8k in total**, less than half of one small project.

---

## 5. Lead sourcing (free), ~150 leads/day, done in batches

Build lists **2-3 days ahead** of sending. Each row needs: first name, company, website, email (if known), city, segment, plus a **notes** column with one fact you noticed. The notes column is what makes the AI draft sharp.

**Real estate, India**
- Google Maps: "real estate agent Noida sector 150", "property dealer Gurgaon Golf Course Road", "real estate consultant Baner Pune". Export with a free scraper extension (e.g. Instant Data Scraper) to get name, website and phone.
- **RERA agent registries** (UP-RERA, MahaRERA, HRERA, K-RERA): public lists of registered agents, often with firm names and emails.
- 99acres/MagicBricks dealer profiles → firm name → website → email.
- Pick firms **with a website** and **active listings/ads**: they already spend on leads, so leaking leads costs them money.
- Notes ideas: "running FB ads for Sector 150 project", "40 listings on 99acres", "only a phone number on site, no WhatsApp".

**Real estate, UAE**
- Bayut and Property Finder agency directories (agency pages show agent count and listing volume). Target 5-50 agents.
- DLD (Dubai Land Department) registered broker lookup; LinkedIn "Managing Director" + "real estate" + Dubai.

**Agencies (India, UK)**
- Clutch.co, GoodFirms, DesignRush: filter by country, team size 2-49, services "digital marketing" / "web development". Skip agencies that already sell "AI automation".
- Signals: a job post for "automation", "n8n", "WhatsApp API" or "Zapier" developer means active demand right now.

**Startups (internships)**
- YC company directory (filter India, recent batches, "hiring"), Wellfound, Inc42/Entrackr funding news from the last 90 days, LinkedIn posts saying "we're hiring interns/founding engineer", Hacker News "Who is hiring".
- Email the **founder or CTO**, not HR. Use the product first, and put one concrete observation in notes (the model builds the email on it).

**Finding emails (free tiers):** the company site's contact/about pages (the pipeline scrapes these automatically), Hunter (free monthly searches), Apollo (free credits), Snov.io (free credits). The pipeline guesses `first.last@domain` patterns but **won't send to guesses** unless you pass `--allow-guessed`. Run guessed or uncertain emails through a verifier's free credits first. Bounces are the #1 way new domains die.

---

## 6. The automation

```
          (you, 2-3x/week)                (cron, 07:30)                  (you, ~20 min)
CSV lists ───────────────► import ──► enrich ──► verify ──► draft ───────────► review
                                     website     MX/role    Claude writes        approve / edit /
                                     signals     scoring    email + 3 follow-ups regenerate / reject
                                                                                     │
       ┌─────────────────────────────────────────────────────────────────────────────┘
       ▼   (cron, every 10 min)                      (cron, every 15 min)
     send ── inside each lead's business hours ──► sync replies ──► classify ──► stop sequence
     2 per run, 7-13 min gaps, warm-up ramp,         IMAP             Claude       suppress unsubscribes
     per-inbox caps, follow-ups threaded,                                          draft reply in Drafts
     auto-pause if bounces >3%                                                    + Telegram ping
```

**What runs without you:** list enrichment, email checks, prioritisation, writing, business-hours scheduling across IST/GST/UK, throttling, warm-up ramp, threaded follow-ups on days 3/7/14, stopping on reply, unsubscribes, bounce protection, reply triage, and drafting your answers.

**What stays with you, deliberately:**
1. **Review (~20 min/day).** Every draft is read by a human. This protects your domains, catches wrong facts, and gives you a daily read on quality. Use `approve --min-confidence 0.85` for the obvious ones and read the rest.
2. **Replies and calls.** Nobody hires an auto-responder.
3. **Adding lead lists** 2-3 times a week.

**Where to run it:** a machine that's on all day. Your laptop works if it stays on from 09:00 to 23:00. A free always-on VM is better: Oracle Cloud Always Free or Google Cloud e2-micro free tier. SMTP over ports 465/587 works on both. See `README.md` and `scripts/crontab.example`.

---

## 7. What good emails look like (the quality bar for your review)

Rules the drafting prompt enforces: 50-110 words; plain text; **no links in email 1**; one specific observation about *them*; one proof point that mirrors their situation; one low-friction question. Never "I hope this finds you well", "intern", "student", "fresher", or "quick call".

**Real estate, India**
> **Subject:** 99acres leads at sharma realty
>
> Hi Rohit,
>
> Sharma Realty has 40+ listings on 99acres and MagicBricks, but the site routes every enquiry to one WhatsApp number. When that number is busy, a 9 pm enquiry usually gets its first reply the next morning, after the buyer has spoken to three other dealers.
>
> For a Delhi-NCR brokerage I built the fix: every portal, Facebook and WhatsApp lead becomes a deal, is assigned to an agent, and shows up as a "call within 30 minutes" reminder on that agent's calendar.
>
> Should I send you a one-page plan for your team? No call needed.

**Agency, UK**
> **Subject:** whatsapp + crm work for your clients
>
> Hi Tom,
>
> Your case studies are mostly lead-gen for trades and clinics, which are exactly the clients who then ask for WhatsApp follow-ups and CRM routing. That work is fiddly and eats margin at UK dev rates.
>
> I build it white-label: most recently a WhatsApp bot and lead-routing system that a 9-person sales team runs on every day. I work UK mornings, send written specs, and charge a fixed price per task.
>
> Is there one small automation job in your backlog you'd trust to a fixed-price trial?

**Startup, internship**
> **Subject:** idea for acme's onboarding
>
> Hi Priya,
>
> Congrats on the seed round. I signed up for Acme, and the empty dashboard after sign-up asks new users to connect a data source before showing any value. A pre-loaded sample workspace would let them see the aha moment first. I've seen that cut setup from 15 minutes to 2 on a dev tool.
>
> I'm an AI Researcher at Caudal AI and I also built and run the production CRM and WhatsApp AI system a brokerage's sales team uses daily.
>
> I'm looking for a product-engineering internship where I ship from week one. Open to 15 minutes, or is someone else on the team better to talk to?

---

## 8. Reply → call → close

**Within 1 hour of a positive reply** (Telegram pings you, and the draft is already in the inbox's Drafts folder): edit and send. Run `python -m outreach report` to see open replies, then `outreach done <id>` when handled.

**If they want the plan:** send a 1-page Google Doc within 24 hours. Sections: *What you have today* (from their site/reply), *What leaks*, *What I'd build* (3-5 bullets), *Timeline* (week 1 demo, week 2 live), *Price* (the fixed number plus the pilot option), *Next step* (a 20-minute call, or "reply yes and I'll send the invoice for milestone 1").

**20-minute discovery call:**
1. (3 min) "Walk me through what happens from the moment a lead enquires to the site visit."
2. (5 min) Where it breaks: how many leads a month, how fast the first reply is, who follows up, what a lost deal is worth.
3. (5 min) "If this worked perfectly in 3 weeks, what would be different?"
4. (5 min) Your plan in plain language, and your proof (show the Loom).
5. (2 min) "I'll send a fixed-price proposal today. If it looks right, we start with a 5-day paid pilot."

**Proposal and terms:** fixed scope, out-of-scope list, 2-3 milestones, 50% upfront (or the pilot paid in full upfront), a written delivery date, and 2 weeks of free fixes after go-live. Get paid through Razorpay/UPI or Wise before you start the work.

**Internship replies:** reply the same day with 3 time slots. Before the interview, prepare one concrete idea for their product and bring it. Ask for a 1-week paid/unpaid trial project if they hesitate. It's the same "pilot" logic.

---

## 9. 30-day calendar (Day 1 = Thu Oct 1)

### Week 0: build the machine (Oct 1-7)
| Day | Do |
|---|---|
| 1 Thu | Buy 2 domains, set up Zoho (4 inboxes), SPF/DKIM/DMARC, redirects. Start warm-up. **Message Puneet** for permission, a testimonial and a referral. |
| 2 Fri | Portfolio page + case study. Cal.com. Update LinkedIn. Record the Loom demo (dummy data). |
| 3 Sat | Install the pipeline (`README.md`), fill `profile.yaml` + `settings.yaml`, create the Telegram bot. Build the first **120 startup leads** with notes. |
| 4 Sun | mail-tester every inbox (aim ≥9/10). Build **250 real-estate India leads**. |
| 5 Mon | **Internship sends start** from Gmail (12/day). First morning review. Build 120 UAE leads. |
| 6 Tue | Build 150 agency leads (India + UK). Manual warm-up exchanges continue. |
| 7 Wed | Dry run for freelance: `send --dry-run`. Check that DNS and Postmaster are clean. |

### Week 1: soft launch (Oct 8-14)
- Freelance sending starts on **Thu Oct 8** at the automatic week-1 cap (12/inbox/day).
- Daily loop: 07:30 drafts ready → review by 09:30 → handle replies at lunch and evening → add leads 3x/week.
- Top 5 leads/day: send a LinkedIn connection request (no pitch).
- **Day 14 checkpoint (Oct 14):** run `report`. Rewrite the segment playbook text for any segment under 3% reply rate, and move quota toward the best segment.

### Week 2: full volume (Oct 15-21)
- Ramp to 25/inbox/day automatically. Follow-ups now make up about half the sends.
- Every positive reply gets a 1-page plan within 24 hours. Aim for **3+ calls** this week.
- For the top 10 startups: send a teardown or prototype as a follow-up reply by hand.

### Week 3: close (Oct 22-30)
- Ramp to 35/inbox/day. Keep lists topped up; the machine now runs itself.
- Convert calls to pilots: the fixed-price 5-day milestone.
- Ask every client and every warm "not now" for one referral.
- **Day 30:** keep what worked. Sequences started late in the month keep following up into November, so replies keep arriving after day 30.

### If you're behind at day 21
- Reply rate under 3%: the problem is targeting or the hook. Tighten the list (only brokers running ads, only agencies hiring), rewrite the offer, and check inbox placement with a seed test.
- Replies but no calls: the offer is too big. Lead with the ₹8-15k / $150-250 pilot.
- Calls but no closes: send proposals the same day, lower the first-milestone price, and add a delivery guarantee.

---

## 10. KPIs to watch (from `python -m outreach report`)

| Metric | Healthy | Action if not |
|---|---|---|
| Bounce rate | < 2% | > 3% pauses the inbox automatically. Verify the list before resuming |
| mail-tester score | ≥ 9/10 | Fix DNS; remove links/signature URLs; slow the ramp |
| Reply rate, per segment | ≥ 5% | Rewrite the hook/offer; re-target the list |
| Positive reply rate | ≥ 2% | Lower-friction CTA; smaller pilot |
| Reply → call | ≥ 50% | Answer faster; send 2 concrete slots + the booking link |
| Call → close | ≥ 30% | Same-day proposal; paid pilot; show the Loom |

---

## 11. Risk and compliance

- **Consent and opt-out:** B2B cold email to business addresses is common practice in India/UAE. The UK/EU rely on "legitimate interest", so keep it relevant, identify yourself, and honour opt-outs immediately. Every email carries a `List-Unsubscribe` header, and any "stop/unsubscribe/not interested" reply is suppressed automatically, including all future imports. For US prospects (CAN-SPAM), add a postal address to the signature.
- **Don't email consumers**, don't buy "verified lists" of personal emails, and don't scrape LinkedIn at scale.
- **Protect your personal Gmail:** it only sends internship emails, capped at 20/day.
- **NDA:** Caudal AI appears only as a title. The prompt forbids elaborating on it.
- **Client data:** never show real client leads or phone numbers in demos or screenshots.
