"""
title: Honcho Memory
author: fubarpie
author_url: https://github.com/fubarpie
funding: none
version: 0.1.0
license: MIT
description: Open WebUI filter that gives every chat persistent, reasoning-based
  memory via Honcho (https://honcho.dev). Injects a <honcho_memory> block of peer
  context (representation, peer card, recent conclusions) into the system prompt on
  every turn (inlet), and saves user + assistant messages to a Honcho session for
  async background reasoning (outlet). One workspace per install, one peer per OWUI
  user, one session per chat. Supports /memory chat commands and per-user opt-out
  via UserValves. Degrades silently: any Honcho failure leaves the chat untouched.
requirements:
"""