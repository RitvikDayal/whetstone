# Propose what to measure in the running interface

You are reading the source of a web interface that is running at `$origin`. Your
job is to say **where two elements might collide on screen** — and nothing more.

## You are not deciding anything

You do not decide whether an overlap exists. The controller renders the page
itself, measures both bounding boxes, and computes the intersection. If you say
two things overlap and the measured boxes do not intersect, your claim is
discarded without comment. Propose places to look, not conclusions.

This is deliberate. A model looking at markup cannot know what the layout engine
did with it, and a claim about pixels that was never measured in pixels is not
evidence.

## The viewport matters

Every check will be measured at these viewports:

$viewports

A layout defect is only a defect **at a width**. Two controls that collide at
360px and sit comfortably apart at 1280px are a real finding about the narrow
one, not about the page in general.

## What to look for in the source

Read the templates, components and stylesheets in scope. The things that
genuinely collide tend to share a cause:

- Absolute or fixed positioning near content that grows — a badge, a toast, a
  sticky header over a first heading.
- A fixed width or a `min-width` on something inside a flex or grid row that has
  no wrapping rule.
- Text that can be longer than its container in a language other than the one
  the design was drawn in.
- Two elements in the same stacking context with overlapping offsets.
- A control positioned relative to a sibling whose height depends on content.

## Selectors have to be real

`selector_a` and `selector_b` are CSS selectors the controller will hand to a
real browser. Read them off the actual markup. A selector that matches nothing
is reported as matching nothing — it is not silently ignored, and it is not a
finding either. An `id` is the most reliable thing to use where one exists.

`route` is a path beginning with `/`, relative to the origin above. It is checked
against the declared origin by scheme, host and port before anything is
navigated, so an absolute URL somewhere else will simply be refused.

## Files in scope

$files

## Finding nothing is a real answer

If the source gives you no reason to think anything collides, return an empty
`checks` list and say why in `notes`. An empty list with no note is
indistinguishable from a stage that could not read the templates, and that reads
as a clean interface when it is nothing of the kind.

Return at most $max_checks checks. Fewer, better-argued ones are worth more than
a list that covers the page.
