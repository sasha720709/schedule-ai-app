# A new SES account is sandboxed: it may only send to and from addresses
# that have been verified by clicking a link AWS emails out. Creating this
# resource sends that email; it isn't usable until the link is clicked.
#
# Sender and recipient are the same address here, so one verification
# covers both ends.
resource "aws_ses_email_identity" "notify" {
  email = var.notify_email
}
