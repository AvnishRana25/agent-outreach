# Growth playbook: everything outside the email engine

The engine sends the emails. This file covers what turns replies into contracts and what finds work that cold email can't. Do the first-week items once; the rest is a weekly rhythm.

## First week (about 3 hours in total)

### 1. Publish the case-study page (20 min)
`site/index.html` is a one-page site built only from facts in your profile.
- **Vercel (free):** Add New → Project → import this repo → set **Root Directory** to `site` → Deploy. You get `something.vercel.app`; rename it under Settings → Domains, e.g. `avnishrana.vercel.app`.
- **Interactive demo:** `site/index.html` includes an interactive pipeline walkthrough with dummy data so visitors can simulate portal, Meta ad, and website lead routing directly in their browser without exposing client data.
- **Spread the link:** put the page URL in `config/profile.yaml` under each signature, and in your LinkedIn "Featured" section.

### 2. Record a 2-minute Loom (30 min)
Use a demo account or dummy data only, never real client data. Script:
1. **(0:00–0:15)** "This is the lead system I built for a real-estate brokerage. Every lead, from portals, ads, the website or WhatsApp, lands here."
2. **(0:15–0:50)** Show a test lead arriving, the instant WhatsApp reply, and the deal created and assigned to an agent with a 30-minute calendar reminder.
3. **(0:50–1:20)** Show the source tag on the deal: "This one came from the Instagram campaign, so the owner can see which ads turn into deals."
4. **(1:20–1:45)** Show a "call me back" turning into a task with an SLA and escalation.
5. **(1:45–2:00)** "I set up a version of this for other teams as a fixed-price, seven-day pilot. Reply to my email if you want a one-page plan."

Use it in replies ("here's a 2-minute walkthrough") and on the page. Don't put it in first emails: links in a cold email hurt delivery.

### 3. Ask Realty Pandit for a testimonial, permission and introductions (10 min)
Send on WhatsApp, edited to sound like you:

> Hi Puneet, quick ask. I'm starting to offer the lead system I built for you to a few other brokerages and agencies. Three things, all optional:
> 1. Could I mention Realty Pandit by name as a client?
> 2. Would you write two lines on what changed for you (faster replies, knowing which ads work, whatever's true)? I'm happy to draft it for you to edit.
> 3. Do you know one or two brokers or channel partners who struggle with leads on WhatsApp? A WhatsApp introduction from you would mean a lot.
>
> Thanks either way, and nothing changes on your system.

If they say yes to the name, replace "a real-estate brokerage in Delhi NCR" on the case-study page and in `profile.yaml` with the name, and add the quote under the stats.

### 4. Upwork and Contra profiles (60 min)
Buyers there expect your price range, and the first contract often comes faster than from cold email.

**Title:** WhatsApp, CRM & AI automation · lead-handling systems that answer and track every lead

**Overview (paste, then adjust):**
> I build lead-handling systems on WhatsApp, CRM and AI, so every enquiry gets an instant reply, the right person calls back within 30 minutes, and you can see which ads actually produce deals.
>
> Recent work, in production for a real-estate brokerage:
> - Every lead from portals, Facebook/Instagram ads, the website and WhatsApp auto-creates a deal, is assigned to an agent, and puts a "call within 30 minutes" reminder on their Google Calendar.
> - Click-to-WhatsApp ad leads tracked to the campaign, after finding why Meta conversion tracking had silently failed for 2.5 months.
> - An LLM classifier recovered the requirements of 722 of 746 blank portal leads (97%).
> - Production errors cut from about 818 a day to near zero.
>
> How I work: a one-page plan with a fixed price within 24 hours, a working demo in the first week, and a short daily update.
>
> Stack: WhatsApp Cloud API, Meta Graph and Ads APIs, n8n/Zapier, TypeScript/Node, Python, PostgreSQL, React/Next.js, LLM APIs.

**Portfolio:** the case-study page plus the Loom. **Rate:** USD 15/hour on the profile, with fixed-price bids. Platform fees take about 10–20%, so this nets roughly your $10–12.

**Daily (20 min):** search "WhatsApp automation", "n8n", "Zapier", "CRM integration", "lead routing" and "AI chatbot". Send 3–5 proposals on jobs posted in the last 24 hours with fewer than 10 proposals. Open with their problem in one sentence and how you'd solve it in two, then one proof line.

## Every week

### LinkedIn (30 min a week)
The engine drafts three posts every Monday from your proof points (Engine tab → "LinkedIn posts this week"). Post two of them, Tuesday and Thursday mornings IST, editing so they sound like you. Reply to every comment the same day. Prospects check your profile after your email, and this is what they'll see.

### The Monday summary
It arrives on Telegram at 09:00 with last week's numbers and specific changes, for example "angle X beats Y in Gulf real estate". Apply the change in `config/settings.yaml` (edit `angles`, `offer` or `daily_new`). The engine uses it from the next run.

### Deals
Keep the **Deals** table in the Respond tab current: set the opportunity type (`contract` vs `internship`), track stage from call booked → proposal sent → won, with value and currency (USD, GBP, AED, INR), or lost. Record the next action and its due date on every conversation. The dashboard automatically flags overdue actions, and the Results tab displays separate contract and internship funnels alongside email segments.

## Turning a reply into a contract
1. **Reply within the hour.** Telegram pings you, and the drafted answer is in the Respond tab.
2. **If they want details,** click **Make 1-page plan**. It's written from their research and reply and priced from the segment's fixed offer. Check it, add it to your reply, and send.
3. **On the call,** confirm the problem, the deadline and who decides. Send the plan with a fixed price the same day.
4. **Start small:** the pilot and its price exactly as in the plan. Get the testimonial and an introduction as soon as it's live.

## Internships: where they actually come from
Cold email to foreign startups rarely leads to an internship, because hiring someone in India adds payroll and paperwork for them. The engine now sends more to Indian startups and offers a paid trial project. Alongside it:
- **Wellfound and Instahyre:** set your profile to "AI / product engineering intern or contract". Apply to 5 roles a day at seed–Series A startups, with a two-line note that links the case study.
- **Referrals:** message 10 people you've worked with (Klimashift, Go4Database, Draftss, HackCBS teammates) asking for one introduction each to a founder or engineering lead who's hiring.
- **Caudal AI:** a recommendation from your current lead carries weight. Mention only your title, never the work under the NDA.

Track applications, platform proposals, and referrals directly in the dashboard alongside email leads: use "Add company" in the Engine tab with source (e.g. `referral`, `upwork`, `wellfound`) and opportunity type (`contract` or `internship`). Keep next-action dates current so overdue follow-ups stay highlighted.

## Your config after this update
New keys (`angles`, `price`, and the new sections) reach your existing `config/settings.yaml` automatically. Your own values always win, so **the new fixed-price offers only apply if you replace your `segments:` block** with the one in `config/settings.example.yaml`. Keep your own `inboxes`. Check the prices first: they're defaults set here, not quotes you've already agreed to.
