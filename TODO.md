* figure out from_api()


* grep -rl `\.post`, `posts_post_change`, and `posts\.`
* grep -rl `accounts\.`
* Figure out .html_content()
* Do we want Address() model? or is that more of Contact Database thing?
* README.md
  * how to install
  * required settings
  * required fields on Post
  * The from_email address you use must use a verified email address for your account
  * Get admin ctct syling (.button, .badget, colors for .bad, .ok, .warn)
  * CTCT_PHYSICAL_ADDRESS = {
      'address_line1': '',
      'address_line2': '',
      'address_optional': '',
      'city': '',
      'country_code': '',
      'country_name': '',
      'organization_name': '',
      'postal_code': '',
      'state_code': '',
    }
  * CTCT_REPLY_TO_EMAIL # Optional, defaults to CTCT_FROM_EMAIL
  * ADMINS?
