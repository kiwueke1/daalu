# 9. Tenants, users, and access control

This chapter explains the access-control model in enough detail
that you can answer questions like "can my colleague see this?",
"who can do that?", and "what happens when someone leaves?"
without guessing.

It's a short chapter because the model is intentionally simple. If
you've come from a tool with deeply nested roles and per-resource
ACLs, you may keep expecting more. The promise of this chapter is
that what you see is all there is.

---

## The three primitives

Daalu's access model has exactly three things in it:

1. **Tenant** — your company's workspace. A row in the `tenants`
   table.
2. **User** — a person's account. A row in the `users` table with
   a foreign key to a tenant.
3. **Role** — a single boolean column on each user, `is_admin`.
   True = admin; false = regular user.

That's it. There are no projects, sub-tenants, teams, groups, or
nested roles. We've considered each of these and rejected them
because the next-cheapest abstraction always cost more than the
problem it solved.

---

## Tenant boundaries

Everything in Daalu is **tenant-scoped**. When you log in, your
session carries a `tenant_id`. Every API call attaches that
tenant ID to the query. The database queries that back the API
include `WHERE tenant_id = $current_tenant` everywhere.

What this means in practice:

- You cannot see another tenant's data, full stop. The Assistant
  cannot answer a question with another tenant's data. The
  inventory page cannot show another tenant's devices.
- Operations that mutate state are also tenant-scoped. Approving
  a change proposal authored in your tenant cannot affect
  another tenant's devices, even if you have its ID.
- The exception is the **Daalu platform team** — the engineers
  who run the product itself. Like any SaaS, our SREs can read
  data when they need to investigate an issue. We log every
  access; the formal policy is in Chapter 57 of the engineer
  book.

If you operate in a regulated environment, this is the section
you give your security reviewer. The implementation behind it is
in the engineer book; the contract is here.

---

## What admins can do that users can't

A reminder from Chapter 6, with one or two clarifications:

| Action | Admin | User |
|---|---|---|
| Invite/remove users | Yes | No |
| Change roles | Yes | No |
| Add/remove integrations | Yes | No |
| Edit tenant settings | Yes | No |
| See *Billing* page | Yes | No |
| Approve proposals | Yes (incl. others') | Yes (not their own) |
| Open proposals | Yes | Yes |
| Use the Assistant | Yes | Yes |
| Investigate | Yes | Yes |
| Mint own PATs | Yes | Yes |
| Mint others' PATs | Yes | No |
| Revoke own PATs | Yes | Yes |
| Revoke others' PATs | Yes | No (admin only) |

The last few rows are subtle: a user can manage *their own*
personal access tokens, including listing them and revoking them.
They cannot see or touch anyone else's PATs. Admins can do both
for everyone.

---

## The four-eyes rule

The only enforced separation-of-duties rule in Daalu is:

> A user cannot approve a change proposal they (or the Assistant
> on their behalf) proposed.

This applies regardless of role. An admin can't approve their own
proposals either.

The point is purely to ensure that *some* second pair of human
eyes look at any state-changing operation. It's not a substitute
for code review, regulatory compliance, or formal change
management — those should layer on top using workflows or
external tooling.

If you need stricter rules (multi-approver, mandatory wait time,
ticket integration), see Chapter 21 on workflows.

---

## A note on "Daalu admin" vs "tenant admin"

Two terms that sound similar but mean different things:

- **Tenant admin** — what this chapter has been talking about.
  A user with `is_admin=true` on your tenant. Hundreds of these
  exist across all our customers; you may be one.
- **Daalu admin** (sometimes "superuser") — a member of the
  Daalu engineering team. They administer the platform itself
  (the operator app, the infrastructure, the inference router).
  They do not see your tenant's data without an explicit support
  ticket, and access is logged.

The two are completely separate identities. A Daalu admin
account has no special powers inside your tenant.

---

## What "access control" doesn't cover

Out of scope of this chapter — but worth flagging so you don't
make assumptions:

- **Cloud-provider permissions.** When you connect AWS, the
  policy on the IAM role you create is what limits what Daalu
  can do in your cloud. The Daalu access-control system has no
  visibility into that policy; it can only refuse to *try* an
  operation, not refuse to *succeed* at one your cloud allowed.
  Always lock down at the cloud-provider level too.
- **Network device permissions.** Same point. The SoT
  integration uses credentials you provided; the device side is
  what enforces what those credentials can do.
- **Audit retention.** What gets logged and how long it's kept
  is configured by your operator.

---

## What's next

Chapter 10 covers integrations and adapters as a concept — the
substrate on which all the inventory and alerting features run.
The other chapters in Part III each go deep on one type of source.
