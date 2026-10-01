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

### 4. Write your AI opinions into `profile.yaml` (15 min)
The weekly LinkedIn drafts argue from them. Add a `linkedin:` section (copy it from `config/profile.example.yaml`) and write 5-10 views you'd defend in a conversation, in your own words: on AI agents, evaluation, open vs closed models, AI in real businesses, what research teaches you about products. Never anything about Caudal AI's work.

## Every week

### LinkedIn (30 min a week)
Every Monday the engine drafts three posts about AI and automation (Engine tab → "LinkedIn posts this week"):
1. **A take on this week's AI news**: one story from Hacker News or Google News, with the link, and your view on what it means for people building with AI.
2. **An opinion on the AI industry**, argued from the opinions in your `profile.yaml`, from the point of view of someone who works in AI research.
3. **A lesson from building** AI or automation systems, sometimes using one of your proof points as the example.

They're general AI posts, not real-estate posts: at most one in three mentions real estate, and only as an example. Open the news link, read every claim, and fix anything flagged "Check before posting" (usually an opinion the AI proposed that isn't in your list). Post two a week, Tuesday and Thursday mornings IST, and reply to every comment the same day.

### The Monday summary
It arrives on Telegram at 09:00 with last week's numbers and specific changes, for example "angle X beats Y in Gulf real estate". Apply the change in `config/settings.yaml` (edit `angles`, `offer` or `daily_new`). The engine uses it from the next run.

### Deals
Keep the **Deals** table in the Respond tab current: set the opportunity type (`contract` vs `internship`), track stage from call booked → proposal sent → won, with value and currency (USD, GBP, AED, INR), or lost. Record the next action and its due date on every conversation. The dashboard automatically flags overdue actions, and the Results tab displays separate contract and internship funnels alongside email segments.

## Turning a reply into a contract
1. **Reply within the hour.** Telegram pings you, and the drafted answer is in the Respond tab.
2. **If they want details,** click **Make 1-page plan**. It's written from their research and reply and priced from the segment's fixed offer. Check it, add it to your reply, and send.
3. **On the call,** confirm the problem, the deadline and who decides. Send the plan with a fixed price the same day.
4. **Start small:** the pilot and its price exactly as in the plan. Get the testimonial and an introduction as soon as it's live.

## Where the leads come from (all free, all fresh)
The engine only takes recent postings: forum and Reddit posts from the last 24 hours, job posts from the last 7 days, funding news from the last 14 days (the caps are under `freshness:` in `settings.yaml`). Anything undated is skipped.

**Internships**
- **Startups' own job boards** (Greenhouse, Lever, Ashby): every startup in your leads is checked for fresh intern, junior and AI-engineer roles. The posting becomes the email's hook ("saw your AI Engineer Intern role"). Apply on their board too.
- **Funding news** (Inc42, Entrackr, YourStory, TechCrunch): startups that raised in the last two weeks are about to hire.
- **GitHub "good first issue" repos**: startups with open beginner tickets. Fix one *before* the email goes out, then mention the PR. Add a free `GITHUB_TOKEN` to `.env` for higher limits.
- **YC directory, Launch HN, HN "Who is hiring"**, and the remote boards (Remotive, Himalayas, RemoteOK, Jobicy, We Work Remotely, Working Nomads).
- **Wellfound and Instahyre** (by hand): set your profile to "AI / product engineering intern or contract" and apply to a few fresh roles a day.
- **Caudal AI:** a recommendation from your current lead carries weight. Mention only your title, never the work under the NDA.

**Contract work**
- **Community boards** (answered by hand, within the hour): the n8n, Bubble and Make forums, and r/forhire, r/n8n, r/automation, r/zapier, r/nocode, r/AI_Agents, r/SaaS, r/hiring. Each new [Hiring] post pings Telegram with a drafted reply.
- **Contract roles** on startups' job boards and the remote boards, plus HN "Seeking freelancer".
- **Agencies that need a builder** (white-label): HubSpot and Webflow partner directories, found with Firecrawl search.
- **Real estate:** the Dubai Land Department broker register, Bayut and Property Finder agency pages, and the Meta Ad Library list in the Engine tab.

Check that every source answers from your Mac with `python -m outreach sources-check` (it fetches each one once and saves nothing). Leads you find yourself go in with "Add company" in the Engine tab, and people found on the Prospects desk with **Send to engine**.

## Your config after this update
New keys (`angles`, `price`, and the new sections) reach your existing `config/settings.yaml` automatically. Your own values always win, so **the new fixed-price offers only apply if you replace your `segments:` block** with the one in `config/settings.example.yaml`. Keep your own `inboxes`. Check the prices first: they're defaults set here, not quotes you've already agreed to.
