# A new SES account is sandboxed: it may only send to and from addresses
# that have been verified by clicking a link AWS emails out. Creating this
# resource sends that email; it isn't usable until the link is clicked.
#
# Sender and recipient are the same address here, so one verification
# covers both ends.
resource "aws_ses_email_identity" "notify" {
  email = var.notify_email
}

# ---------------------------------------------------------------------------
# A domain to send FROM, so the mail is signed rather than merely accepted
#
# The bug this closes, with the evidence, because it looked like a code
# problem for two rounds and was not one:
#
#   Sender and recipient were the same gmail.com address, so every
#   notification claimed a gmail.com origin and arrived from AWS. gmail.com
#   publishes `v=spf1 redirect=_spf.google.com`, which does not include SES,
#   so all of them failed SPF. Its DMARC policy is `p=none`, so Gmail accepted
#   them rather than bouncing -- CloudWatch AWS/SES showed Send 8, Delivery 8,
#   Bounce 0, Complaint 0 -- and filed all eight in spam.
#
# Easy DKIM on a domain we control fixes it properly: the mail is signed with
# a key published in our own DNS, so it passes DKIM *and* aligns with the From
# domain, which is what DMARC actually asks for. A custom MAIL FROM subdomain
# is deliberately NOT added -- DKIM alignment alone satisfies DMARC, and every
# extra DNS record is another thing to get wrong in a zone that is already
# serving a live website.
#
# Gated on `sender_domain` being set. Empty keeps the previous behaviour
# exactly, so this cannot break a deployment that has no domain.
# ---------------------------------------------------------------------------

# The zone is named rather than looked up, on purpose.
#
# `data "aws_route53_zone"` would need ListHostedZones, GetHostedZone *and*
# ListTagsForResource -- the provider reads tags whether you want them or not
# -- which is three account-wide permissions granted to save typing an id that
# never changes. Naming it keeps the deploy user's Route 53 access to writing
# records in one zone, which is the same scoping argument as every other
# policy in this project.

resource "aws_ses_domain_identity" "sender" {
  count = var.sender_domain == "" ? 0 : 1

  domain = var.sender_domain
}

resource "aws_ses_domain_dkim" "sender" {
  count = var.sender_domain == "" ? 0 : 1

  domain = aws_ses_domain_identity.sender[0].domain
}

# The only records this adds: three CNAMEs under `_domainkey`, which is a name
# nothing else uses. It cannot collide with the A records already serving the
# website on this domain, and it does not touch MX, SPF or DMARC.
resource "aws_route53_record" "sender_dkim" {
  count = var.sender_domain == "" ? 0 : 3

  zone_id = var.sender_zone_id
  name    = "${aws_ses_domain_dkim.sender[0].dkim_tokens[count.index]}._domainkey.${var.sender_domain}"
  type    = "CNAME"
  ttl     = 600
  records = ["${aws_ses_domain_dkim.sender[0].dkim_tokens[count.index]}.dkim.amazonses.com"]
}

# A DMARC record, added once the zone could actually be looked at.
#
# The earlier reasoning refused this sight-unseen, on the grounds that every
# extra record in a live zone is a risk. Reading the zone settled it: there is
# no TXT of any kind and no `_dmarc` name, so this collides with nothing.
#
# `p=none` asks receivers to do nothing differently -- it is not a policy, it
# is a statement that the domain is configured on purpose. Without any DMARC
# record at all, a receiver has nothing to evaluate the DKIM alignment
# *against*, which is the entire point of signing in the first place.
#
# No SPF record is added, deliberately. Without a custom MAIL FROM subdomain
# SES uses its own envelope domain, so SPF is checked against amazonses.com
# and an SPF record here would be consulted by nobody -- a record that does no
# work is a record that misleads whoever reads the zone next.
resource "aws_route53_record" "sender_dmarc" {
  count = var.sender_domain == "" ? 0 : 1

  zone_id = var.sender_zone_id
  name    = "_dmarc.${var.sender_domain}"
  type    = "TXT"
  ttl     = 600
  records = ["v=DMARC1; p=none;"]
}

# Blocks until AWS has seen the DKIM records and marked the domain verified,
# so a `terraform apply` that succeeds means the next email is actually signed
# -- rather than succeeding and leaving the Notifier sending from an identity
# SES will refuse.
resource "aws_ses_domain_identity_verification" "sender" {
  count = var.sender_domain == "" ? 0 : 1

  domain     = aws_ses_domain_identity.sender[0].id
  depends_on = [aws_route53_record.sender_dkim]
}
