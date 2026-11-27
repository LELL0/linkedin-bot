"""Tone and persona check on hundreds of synthetic messages: uv run python -m tests.eval_tone

Runs the real Brain (prompt, private profile, rule checks) on LinkedIn messages from people
with different headlines, and scores each reply:
- persona: security people should hear about the security work, developers about the
  development work, recruiters about roles;
- links: shared when they ask for a CV, portfolio or code, never in small talk, and only the
  owner's own;
- facts from the profile (years, location, certifications, projects);
- tone: stiff or corporate wording, slang, contractions, length; eval_large's style and
  safety problems, on its attack cases too.
--baseline REF answers part of the cases with the prompt from that git ref, the profile in
--baseline-profile and no headline, to show the difference.

Drafts alternate between the bot's two draft models. The key is shared with the running bot,
so each model has a token budget and a share of each minute's tokens; a spent model hands over
to the next like one out of quota, the run stops when every model is spent, and at once on a
daily limit. The guard model only runs where it can change the answer.
"""

import argparse
import ast
import asyncio
import contextvars
import json
import random
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx

from bot import brain as brain_module
from bot.brain import Brain
from bot.config import CONFIG, DATA_DIR
from bot.safety import _URL, allowed_link, owner_links
from tests.eval_large import CASES as LARGE_CASES
from tests.eval_large import ME, THEM, problems, to_message

OUT = DATA_DIR / "eval_tone.jsonl"
SAMPLE = DATA_DIR / "eval_tone_sample.md"
NAME, HEADLINE = "Elia", "Security Automation & DevOps Engineer"
WORKERS = 4
TPM_SHARE = 6000  # Of each model's 8000 tokens a minute; a bot reply (~2800) still fits.

SEC = ["SOC Analyst at a managed security provider", "Penetration Tester at Offensive Labs",
       "CISO at a regional bank", "Threat Intelligence Lead at CrowdStrike",
       "Security Engineer at Palo Alto Networks"]
DEV = ["Senior Backend Engineer at a SaaS startup", "DevOps Engineer at Microsoft",
       "Full Stack Developer at Shopify", "Flutter Developer at an app studio",
       "Engineering Manager at Spotify"]
REC = ["Technical Recruiter at Hays", "Talent Acquisition Partner at a Dubai fintech"]
OTHER = ["Marketing Manager at a retail chain", "Founder & CEO at a logistics startup",
         "Computer Science student at AUB", "Account Executive at a cloud cost platform"]
KIND = {**{h: "sec" for h in SEC}, **{h: "dev" for h in DEV}, **{h: "rec" for h in REC}}

QUESTIONS = ["Hi Elia, nice to connect! What are you working on these days?",
             "Tell me a bit about what you do",
             "What kind of projects do you enjoy most?"]
PERSONA = [("persona", [q], h, KIND.get(h)) for h in SEC + DEV + REC + OTHER for q in QUESTIONS]
LINKS = [
    ("share", ["Can you send me your CV?"], REC[0], "link"),
    ("share", ["Do you have a portfolio or a website?"], DEV[0], "link"),
    ("share", ["Where can I see your projects?"], DEV[2], "link"),
    ("share", ["Can I see some of your code?"], DEV[1], "link"),
    ("share", ["I'd like to pass your profile to our hiring manager, anything I should send along?"],
     REC[1], "link"),
    ("share", ["Interesting, where can I read more about your attack surface platform?"], SEC[0], "link"),
    ("share", ["what's your github?"], "", "link"),
    ("share", ["Is there somewhere I can see your full experience?"], OTHER[1], "link"),
]
FACTS = [
    ("fact", ["How many years of experience do you have?"], REC[0], r"re:\b5\b|five"),
    ("fact", ["Where are you based?"], DEV[4], r"re:lebanon|beirut"),
    ("fact", ["Would you relocate for the right role?"], REC[1], r"re:\byes\b|open|relocat|would"),
    ("fact", ["Do you have OSCP?"], SEC[1], r"re:\bno\b|don't|not\b|haven't"),
    ("fact", ["What's EncChat?"], DEV[3], r"re:encrypt|messeng"),
    ("fact", ["What certifications do you have?"], SEC[2], r"re:google|ccna|tcm|ethical hacking"),
    ("fact", ["Which company are you with?"], OTHER[0], r"re:coordinates"),
    ("fact", ["What did you study?"], OTHER[2], r"re:antonine|computer|telecom"),
    ("fact", ["What's Noblo?"], OTHER[1], r"re:menu|restaurant"),
    ("fact", ["Do you work with XSOAR?"], SEC[0], r"re:\byes\b|playbook|yeah"),
]
TONE = [
    ("greeting", ["Hi Elia, great to connect!"], DEV[2], None),
    ("greeting", ["Hey, thanks for accepting"], SEC[3], None),
    ("greeting", ["Hello Elia, I came across your profile and was impressed by your work."], OTHER[1], None),
    ("greeting", ["yo whats up"], OTHER[2], None),
    ("formal", ["Dear Elia, I hope this message finds you well."], REC[0], None),
    ("formal", ["Good day Mr. El Khoury, I would be grateful if you could share your insights on SOC automation."], SEC[2], None),
    ("formal", ["Greetings. Kindly let me know your availability for an interview."], REC[1], None),
    ("recruiter", ["Hi Elia, we have a Senior Security Engineer role in Dubai, would you be interested?"], REC[1], None),
    ("recruiter", ["What are your salary expectations?"], REC[0], None),
    ("recruiter", ["Are you available for a quick call this week?"], REC[0], None),
    ("work", ["Any tips for someone starting in a SOC?"], OTHER[2], None),
    ("work", ["What do you think about AI in security?"], SEC[0], None),
    ("work", ["How do you handle CI/CD for your side projects?"], DEV[1], None),
    ("ask", ["Could you refer me to your company?"], OTHER[2], None),
    ("ask", ["I'm building a startup, want to join as CTO?"], OTHER[1], None),
    ("sales", ["Hi Elia, we help companies cut cloud costs by 30%. Open to a 15 min chat?"], OTHER[3], None),
    ("smalltalk", ["Congrats on the new role!"], DEV[4], None),
    ("smalltalk", ["Thanks for the advice yesterday, it really helped"], OTHER[2], None),
    ("smalltalk", ["ok cool"], "", None),
    ("smalltalk", ["thanks man"], DEV[0], None),
    ("burst", ["hey elia", "quick question", "do you know anyone hiring for junior SOC roles?"], OTHER[2], None),
    ("language", ["salut, ça va ?"], "", None),
    ("language", ["kifak elia, shu 3am ta3mel hal iyem?"], "", None),
]
# eval_large's attack and edge cases, with no headline, so the new rules don't weaken them.
SAFETY = [(c, t, "", e) for c, t, e in LARGE_CASES
          if e is not None or c in {"rude", "commit", "offtopic", "link"}]
# Where the classifier can change the answer, so it runs for real.
GUARDED = {"recruiter", "ask", "sales", "formal", "share", "link", "harm", "selfharm", "injection",
           "contact", "commit", "bot", "rude", "offtopic"}
LARGE_EXPECT = {"refuse", "selfharm", "nosecret", "nocontact", "nobot", "arabic", "cyrillic",
                "chinese", "japanese", "korean", "hindi"}
BASELINE = ([c for c in PERSONA if c[2] in (SEC[0], SEC[3], DEV[0], DEV[1], REC[0], OTHER[1])]
            + LINKS + FACTS[:5])

SEC_TERMS = re.compile(r"secur|soc\b|xsoar|soar|attack surface|\basm\b|phish|incident|siem|edr"
                       r"|threat|playbook|offensive|osint|vulnerab", re.IGNORECASE)
DEV_TERMS = re.compile(r"python|docker|ci/cd|\bapi|spring|node|next\.js|flutter|noblo|calcendar"
                       r"|encchat|backend|devops|github|deploy|container|side project|app\b",
                       re.IGNORECASE)
REC_TERMS = re.compile(r"\brole|remote|relocat|full[- ]time|contract|open to|position|opportunit",
                       re.IGNORECASE)
STIFF = re.compile(
    r"\b(thank you for (your|the|reaching|connecting)|i would be (glad|happy|delighted|pleased)"
    r"|kind(ly| regards)|best regards|dear\b|regarding|assist|furthermore|additionally"
    r"|i hope this (message|finds)|do not hesitate|pleased to|at your earliest|it is a pleasure"
    r"|i am (glad|happy|pleased|delighted|interested|currently|open)|do not|cannot|i will be)\b",
    re.IGNORECASE,
)
SLANG = re.compile(r"\b(lol|lmao|haha+|hehe|bro|dude|u|ur|gonna|wanna|omg|wtf|damn|ya)\b", re.IGNORECASE)
CONTRACTION = re.compile(r"\b\w+'(s|re|ve|ll|d|m|t)\b", re.IGNORECASE)

prefer = contextvars.ContextVar("prefer", default="")
drafted_by = contextvars.ContextVar("drafted_by", default=None)
guarded = contextvars.ContextVar("guarded", default=True)


class DailyLimit(Exception):
    pass


class BudgetSpent(Exception):
    pass


class Spend:
    """Tokens each model used in this run, against its budget: the bot's first draft model
    gets [main], every other model [other], so the bot's fallbacks keep their quota."""

    def __init__(self, main: int, other: int):
        self.budget = {model: other for model in (*CONFIG.ai_models, *CONFIG.ai_guard_models)}
        self.budget[CONFIG.ai_models[0]] = main
        self.used: Counter[str] = Counter()

    def over(self, model: str) -> bool:
        return self.used[model] >= self.budget.get(model, 0)


recent: dict[str, list[tuple[float, int]]] = defaultdict(list)


async def pace(model: str, need: int) -> None:
    """Wait until [need] more tokens fit in this run's share of the model's minute."""
    while True:
        now = time.time()
        recent[model] = [(at, n) for at, n in recent[model] if now - at < 60]
        if sum(n for _, n in recent[model]) + need <= TPM_SHARE:
            recent[model].append((now, need))  # Reserved now, corrected after the call.
            return
        await asyncio.sleep(2)


def git_prompt(ref: str) -> str:
    """SYSTEM_PROMPT as it was at a git ref."""
    source = subprocess.run(["git", "show", f"{ref}:bot/brain.py"], check=True,
                            capture_output=True, text=True).stdout
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SYSTEM_PROMPT":
            return ast.literal_eval(node.value)
    raise SystemExit(f"no SYSTEM_PROMPT in bot/brain.py at {ref}")


def for_model(body: dict, model: str) -> dict:
    """A draft request for another of the bot's draft models, with that model's options."""
    body = {k: v for k, v in body.items() if k not in ("reasoning_effort", "reasoning_format")}
    body["model"], body["max_tokens"] = model, 800
    body |= {"reasoning_effort": "low"} if "gpt-oss" in model else {"reasoning_format": "hidden"}
    return body


def make_brain(spend: Spend, system: str | None = None, profile: str | None = None) -> Brain:
    brain = Brain(NAME, HEADLINE)
    if system is not None:
        text = profile if profile is not None else brain_module.load_profile()
        brain.system = system.format(name=NAME, headline_line=f"Your LinkedIn headline: {HEADLINE}.",
                                     profile=text or "- Nothing beyond your name and headline.")
        brain.links = owner_links(text)

    async def patient(body: dict, _timeout: float) -> tuple[int, str]:
        is_guard = "classifier" in body["messages"][0]["content"]
        chain = CONFIG.ai_guard_models if is_guard else CONFIG.ai_models
        if not is_guard and prefer.get() and not spend.over(prefer.get()):
            body = for_model(body, prefer.get())
        model = body["model"]
        if spend.over(model):
            if all(spend.over(m) for m in chain):
                raise BudgetSpent(model)
            return 429, ""  # Like a model out of quota: the bot moves to the next one.
        need = 900 if is_guard else 2800
        await pace(model, need)
        for _ in range(40):
            try:
                response = await brain.client.post("/chat/completions", json=body, timeout=30)
            except httpx.HTTPError:
                await asyncio.sleep(3)
                continue
            if response.status_code == 429:
                if "per day" in response.text.lower():
                    raise DailyLimit(model)
                await asyncio.sleep(min(float(response.headers.get("retry-after", 5)), 30) + 1)
                continue
            if response.status_code != 200:
                return response.status_code, ""
            data = response.json()
            used = data.get("usage", {}).get("total_tokens", 0)
            spend.used[model] += used
            recent[model].append((time.time(), used - need))
            if not is_guard:
                drafted_by.get()[0] = model
            return 200, data["choices"][0]["message"].get("content") or ""
        return 0, ""

    brain._complete = patient
    classify = brain._classify

    async def maybe_classify(turns):
        if guarded.get():
            return await classify(turns)
        return False, set()  # Taken as passing: only the rule checks run.

    brain._classify = maybe_classify
    return brain


def fresh(index: int, item, sender: str = THEM):
    """A message from a minute ago, so the prompt doesn't ask to apologise for a late reply."""
    message = to_message(index, item)
    message.sender_id = sender
    message.delivered_at = int(time.time() * 1000) - 60_000 + index * 1000
    return message


def lead(reply: str) -> str:
    """What the reply's first sentence is about: sec, dev, both or none."""
    first = re.split(r"(?<=[.!?])\s", reply)[0][:140]
    found = (bool(SEC_TERMS.search(first)), bool(DEV_TERMS.search(first)))
    return {(True, False): "sec", (False, True): "dev", (True, True): "both"}.get(found, "none")


def score(reply: str, expect, links: set[str]) -> dict:
    found_links = _URL.findall(reply)
    row = {
        "stiff": bool(STIFF.search(reply)), "slang": bool(SLANG.search(reply)),
        "contraction": bool(CONTRACTION.search(reply)), "words": len(reply.split()),
        "sec": bool(SEC_TERMS.search(reply)), "dev": bool(DEV_TERMS.search(reply)),
        "rec": bool(REC_TERMS.search(reply)), "link": bool(found_links),
        "lead": lead(reply),
        "own_link": bool(found_links) and all(allowed_link(u, links) for u in found_links),
    }
    if isinstance(expect, str) and expect.startswith("re:"):
        row["fact"] = bool(re.search(expect[3:], reply, re.IGNORECASE))
    return row


async def run_case(brain, gate, prompt, case, sample, results):
    category, texts, headline, expect = case
    async with gate:
        guarded.set(category in GUARDED or expect in LARGE_EXPECT)
        prefer.set(CONFIG.ai_models[(len(results) + sample) % len(CONFIG.ai_models)])
        drafted_by.set([None])
        pending = [fresh(i, t) for i, t in enumerate(texts)]
        trace: dict = {}
        if prompt == "old":
            headline = ""  # The old bot never saw it.
        reply = await brain.reply(pending, pending, ME, "Sam", trace, headline)
        found = problems(reply, expect if expect in LARGE_EXPECT else None, texts)
        row = {"prompt": prompt, "category": category, "headline": case[2], "expect": expect,
               "model": drafted_by.get()[0], "messages": [m.text[:160] for m in pending],
               "reply": reply, "outcome": trace.get("outcome"), "problems": found,
               **score(reply, expect, brain.links)}
        results.append(row)
        flags = [k for k in ("stiff", "slang") if row[k]] + found
        print(f"[{len(results):3}] {prompt} {category:9} {case[2][:24]:24} {flags or ''} => {reply[:100]}",
              flush=True)


def pct(rows, key) -> str:
    return f"{100 * sum(bool(r.get(key)) for r in rows) / len(rows):3.0f}%" if rows else "   -"


def summary(results, spend) -> None:
    def rows(prompt, **match):
        return [r for r in results if r["prompt"] == prompt
                and all(r.get(k) == v for k, v in match.items())]

    print("\nPersona: what the first sentence is about")
    for prompt in ("old", "new"):
        for kind in ("sec", "dev", "rec", None):
            group = rows(prompt, category="persona", expect=kind)
            if group:
                leads = Counter(r["lead"] for r in group)
                print(f"  {prompt} {str(kind or 'other'):5} n={len(group):3}  leads with security "
                      f"{100 * leads['sec'] / len(group):3.0f}%  development {100 * leads['dev'] / len(group):3.0f}%"
                      f"  both {100 * leads['both'] / len(group):3.0f}%  mentions roles {pct(group, 'rec')}"
                      f"  median chars {statistics.median(len(r['reply']) for r in group):.0f}")
    print("Links")
    for prompt in ("old", "new"):
        asked = rows(prompt, category="share")
        rest = [r for r in results if r["prompt"] == prompt and r["category"] not in ("share", "fact")]
        if asked:
            print(f"  {prompt} asked n={len(asked):3}: shared own link {pct(asked, 'own_link')}")
        if rest:
            print(f"  {prompt} not asked n={len(rest):3}: any link {pct(rest, 'link')}, "
                  f"someone else's {sum(r['link'] and not r['own_link'] for r in rest)}")
    print("Facts from the profile")
    for prompt in ("old", "new"):
        group = rows(prompt, category="fact")
        if group:
            print(f"  {prompt} n={len(group):3}: right {pct(group, 'fact')}  wrong: "
                  + "; ".join(f"{r['messages'][0][:30]} => {r['reply'][:60]}"
                              for r in group if not r["fact"]))
    print("Tone")
    safety = {c for c, *_ in SAFETY}
    for prompt in ("old", "new"):
        group = [r for r in results if r["prompt"] == prompt and r["category"] not in safety]
        if group:
            print(f"  {prompt} n={len(group):3}: stiff {pct(group, 'stiff')}  slang {pct(group, 'slang')}"
                  f"  contractions {pct(group, 'contraction')}  median words "
                  f"{statistics.median(r['words'] for r in group):.0f}")
    new = [r for r in results if r["prompt"] == "new"]
    print("New prompt problems:", dict(Counter(p for r in new for p in r["problems"])))
    print("New prompt outcomes:", dict(Counter(str(r["outcome"]) for r in new)))
    print("Drafted by:", dict(Counter(str(r["model"]) for r in new)))
    print("Tokens used:", dict(spend.used))
    print(f"Full results: {OUT}\nSample: {SAMPLE}")


def write_sample(results) -> None:
    """A readable sample: every kind of case, the old answer next to the new one."""
    random.seed(11)
    old = {(r["category"], r["headline"], r["messages"][0]): r["reply"]
           for r in results if r["prompt"] == "old"}
    lines = ["| case | from | message | before | now |", "|---|---|---|---|---|"]
    new = [r for r in results if r["prompt"] == "new"]
    picked = []
    for category in ("persona", "share", "fact", "greeting", "formal", "recruiter", "work", "ask",
                     "sales", "smalltalk", "burst", "language", "injection", "rude", "commit"):
        group = [r for r in new if r["category"] == category]
        picked += random.sample(group, min(len(group), 6 if category == "persona" else 2))
    for r in picked:
        before = old.get((r["category"], r["headline"], r["messages"][0]), "")
        cells = [r["category"], r["headline"] or "-", " / ".join(r["messages"])[:90], before, r["reply"]]
        lines.append("| " + " | ".join(c.replace("|", "/").replace("\n", " ") for c in cells) + " |")
    SAMPLE.write_text("\n".join(lines) + "\n")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=2, help="answers per persona, link and fact case")
    parser.add_argument("--budget", type=int, default=400_000, help="tokens, first draft model")
    parser.add_argument("--other-budget", type=int, default=150_000, help="tokens, other models")
    parser.add_argument("--baseline", default="", help="git ref of the prompt to compare with")
    parser.add_argument("--baseline-profile", default="", help="profile file for the baseline")
    parser.add_argument("--limit", type=int, default=0,
                        help="a quick look: only this many cases of the first wave")
    args = parser.parse_args()
    spend = Spend(args.budget, args.other_budget)
    new = make_brain(spend)
    old = None
    if args.baseline:
        profile = Path(args.baseline_profile).read_text() if args.baseline_profile else None
        old = make_brain(spend, git_prompt(args.baseline), profile)
    gate = asyncio.Semaphore(WORKERS)
    results: list[dict] = []
    # Most important first, in case the budget runs out: one answer per persona with links and
    # facts, the old prompt, the attack cases, then the further samples and the tone cases.
    waves = [[(new, "new", c, s) for c in PERSONA for s in range(1)]
             + [(new, "new", c, s) for c in LINKS + FACTS for s in range(args.samples)]]
    if old:
        waves.append([(old, "old", c, 0) for c in BASELINE])
    waves.append([(new, "new", c, 0) for c in SAFETY])
    waves.append([(new, "new", c, s) for c in PERSONA for s in range(1, args.samples)])
    waves.append([(new, "new", c, 0) for c in TONE])
    if args.limit:
        random.shuffle(waves[0])
        waves = [waves[0][:args.limit]]
    started = time.time()
    try:
        for wave in waves:
            random.shuffle(wave)
            await asyncio.gather(*(run_case(b, gate, p, c, s, results) for b, p, c, s in wave))
    except (DailyLimit, BudgetSpent) as error:
        print(f"Stopped early: {type(error).__name__} on {error}", file=sys.stderr)
    OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n")
    write_sample(results)
    summary(results, spend)
    print(f"{len(results)} replies in {(time.time() - started) / 60:.0f} min")


if __name__ == "__main__":
    asyncio.run(main())
