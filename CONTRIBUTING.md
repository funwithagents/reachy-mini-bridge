# Contributing

Thanks for your interest in `reachy-mini-bridge`. This page says how the project is run today, so you know what to expect before you spend time on it.

## Where the project stands

The bridge is developed by one person, in the open, as a spec-driven project: every change starts as a design in [specs/](specs/), becomes an implementation plan in [plans/](plans/), and only then code ([AGENTS.md](AGENTS.md) describes the workflow). It is at an early stage. The design is still moving, the API changes without a deprecation period, and the code has been exercised on a Reachy Mini Lite and the simulator only, not on the wireless Reachy Mini (see [Project status](README.md#project-status) in the README).

## Pull requests: not yet

External pull requests are not being accepted for now. This is not a judgement on contributions: while the design is settling, a change written against yesterday's specs usually costs more to reconcile than to redo, and reviewing it carefully takes time away from the design work that would make it unnecessary. Rather than let a pull request sit, it will be closed with thanks and, when the idea is a good one, turned into an issue so it is not lost.

This will change once the API stabilises. This file will say so when it does.

## Feedback: yes, please

Issues are the way in, and every kind of feedback is welcome at [github.com/funwithagents/reachy-mini-bridge/issues](https://github.com/funwithagents/reachy-mini-bridge/issues):

- **Bug reports.** Say what you ran (the backend and target: Lite over USB, wireless, sim or fake; the OS; the `reachy-mini` version; your config with any secrets removed), what you expected, what happened, and the log output. A small script that reproduces it is the best possible report.
- **Reports from a wireless Reachy Mini.** The author has none to test on, so a report that the bridge works, or does not, on one is especially valuable, even when everything went fine.
- **API and design feedback.** What you would want a verb, a config field or the motion behaviour to do differently, and why. At this stage the design is cheap to change and this is the most useful input there is. Pointing at the spec you are reacting to helps.
- **Questions.** If the README or a spec left you guessing, that is a documentation bug worth an issue too.

## Working with the code anyway

The code is MIT-licensed, so forking it and building on it is welcome. The commands to lint, type check and run the two test tiers are in the README's [Development](README.md#development) section, and [AGENTS.md](AGENTS.md) explains how the specs, plans and their statuses fit together. If your fork grows something you think belongs upstream, open an issue describing it: that is the conversation that will lead to accepting pull requests later.
