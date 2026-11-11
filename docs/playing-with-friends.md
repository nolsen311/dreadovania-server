<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Playing with friends

One Dreadovania server can have several accounts: yours (the admin) and one for
each friend you invite ([ADR 0020](adr/0020-accounts.md)). Each account's seeds,
presets, game files and Switches are its own; nobody can see anyone else's.

## Inviting a friend

1. On **Account**, under **Accounts**, create an invite link. Add a note saying
   who it's for, if you like.
2. Send the link to your friend. It works once, for 7 days, and is shown only
   when you create it. If it's lost, revoke it and create another.
3. Your friend opens the link, chooses a username and password, and is signed
   in. They can add a passkey and pair their own Switch from their **Account**
   page.

To patch seeds, your friend uploads the game files from **their own** copy of the
game on their **Game files** page. Never share yours: patched seeds are made from
the player's own dump, for their own use ([PLAN.md section
3](PLAN.md#3-user-supplied-content-and-the-legal-line)). Without game files they
can still generate seeds and export presets.

Only you can invite, list and remove accounts. Removing an account deletes
everything in it, after you type its username to confirm.

## Racing the same seed

A seed's **permalink** describes it completely: the settings and the seed
number. Generating from the same permalink gives the same seed, with the same
hash, on any account.

1. One of you generates a seed. For a race, tick **Race seed**: it's generated
   without a spoiler, and its permalink says so, so your friend's copy has no
   spoiler either.
2. On the seed's page, press **Copy permalink** and send it to the other player.
3. They choose **Permalink** under **Generate**, paste it, and generate.
4. Check that both seed pages show the same **Hash**, then each of you patches
   the seed with your own game files and installs it.
