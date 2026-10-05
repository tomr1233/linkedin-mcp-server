# LinkedIn MCP Server

This server reads LinkedIn through one signed-in browser session and returns
what that session can already see. This glossary fixes the words used for the
people and surfaces it reads, so that code, tools and docs name the same thing
the same way.

## Language

### Engagement

**Post**:
A single LinkedIn update, reachable by permalink in either the
`/feed/update/<urn>/` or the `/posts/<slug>` form.
_Avoid_: update, share, activity (all are LinkedIn's own internal names for
parts of a post, not for the post itself)

**Reactor**:
A person whose reaction is listed on a post.
_Avoid_: liker (a Like is only one of LinkedIn's reaction types, so "liker"
names the group by its most common member)

**Commenter**:
A person who wrote at least one comment or reply on a post.

**Comment**:
One piece of text a person wrote under a post, whether it sits at the top level
or as a reply inside a thread.
_Avoid_: reply (a reply is a comment; the distinction does not survive into the
result)

**Engagement row**:
One person's whole relationship to the posts that were read: their name and
profile, whether they reacted, whether they commented, the posts it happened on,
and the comments they wrote.
_Avoid_: engager, participant

**Anonymous reactor**:
A reactor LinkedIn will not name to this session because they are out of
network, shown as "LinkedIn Member" with no profile link.

### Reading

**Section**:
One named block of text in a result, produced by exactly one page navigation.

**Reference**:
One structured entry extracted alongside a section, naming something the
section's text mentions — a person, a company, a post, and now an engagement
row.

**Navigation**:
One page load against LinkedIn. The unit that LinkedIn counts and that the
operations playbook budgets, which is why a section never combines two.

### Messaging

**Conversation**:
One person's thread with the signed-in member, as the inbox lists it: who it is
with, when it last moved, and the opening of its most recent message.
_Avoid_: thread (LinkedIn's internal name for the same thing, and the word the
server uses for the opaque id that addresses one)

**Inbox**:
The list of Conversations the signed-in member can see, newest first.

**Snippet**:
The preview of a Conversation's most recent message, as the inbox shows it.
Not the message: the inbox truncates it, and reading the whole thing is a
separate Navigation.
_Avoid_: preview, last message

**Label**:
A marker LinkedIn puts on a Conversation to say how it arrived rather than who
it is with — Sponsored, InMail.

## Relationships

- A **Post** has zero or more **Reactors** and zero or more **Commenters**
- A **Commenter** writes one or more **Comments** on a **Post**
- An **Engagement row** belongs to exactly one person and spans one or more
  **Posts**
- A person who is both a **Reactor** and a **Commenter** has one **Engagement
  row**, not two
- An **Anonymous reactor** has no **Engagement row**, only a count
- An **Inbox** lists zero or more **Conversations**
- A **Conversation** has exactly one **Snippet** and zero or one **Label**

## Example dialogue

> **Dev:** "If someone reacts to two of the posts we read and comments on one of
> them, how many **Engagement rows** is that?"
>
> **Domain expert:** "One. The row says they reacted and commented, and lists
> both **Posts**. Two rows would read as two people."
>
> **Dev:** "And if they replied inside someone else's thread instead of
> commenting on the post directly?"
>
> **Domain expert:** "Still a **Commenter**. A reply is a **Comment** — they
> engaged with the post either way."
>
> **Dev:** "What about the ones showing as 'LinkedIn Member'?"
>
> **Domain expert:** "Those are **Anonymous reactors**. We can't name them or
> reach them, so they're a count, not a row."

## Flagged ambiguities

- "liker" and "reactor" were used interchangeably while designing
  `get_post_engagement` — resolved: **Reactor** is the term; a Like is one
  reaction type among several.
- "profile URL" was used to mean both a person's profile and a post permalink
  passed to the same argument — resolved: both are accepted as input, but a
  **Post** permalink and a person's profile are distinct things, and a company
  page is neither.
