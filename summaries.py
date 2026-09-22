"""
summaries.py -- cached AI player summaries. See PLAYER_DEV_SPEC.md 9.2 and 9.3.

The rule this module exists to enforce: a player summary costs ONE API call when
new data lands, not one per page view. It is stored against a hash of what it was
written from -- the player's latest session plus the change events and goals in
play -- so an unchanged player returns cached text with no model call at all.

Two halves, deliberately separated so the expensive one is the only one that
needs an API key:

    build_context()    pure, testable, no network. Assembles the compact
                       summary of a player from what the database already
                       computed -- never raw swings or pitches.
    generate()         calls the model once with that context and stores it.

    python summaries.py                weekly run: everyone with new data
    python summaries.py --all          everyone, regardless of new data
    python summaries.py --player 14
    python summaries.py --dry-run      show the context, make no API call
"""

import hashlib
import json
import os
import sys
from datetime import date, timedelta

from sqlalchemy import delete, func, insert, select

import db
import metrics

MODEL = "claude-opus-5"

PITCHING_KNOWLEDGE = """<rapsodo_metrics>
Units and meaning:

  velocity                mph
  spin_rate               rpm, total spin including the gyro component
  true_spin               rpm, the portion actually moving the ball
  spin_efficiency         percent. 100 is pure backspin, 0 is pure gyro
  spin_axis               DEGREES, 0-359, and circular. Divide by 30 to read it as
                          a clock: 0 is 12:00, 30 is 1:00, 180 is 6:00, 270 is
                          9:00. 359 and 1 are two degrees apart, not 358. Never
                          average it arithmetically and never compare it across
                          pitchers
  gyro_degree             degrees of tilt out of the movement plane
  induced_vertical_break  inches, positive is ride, negative is drop
  horizontal_break        inches, signed for direction
  total_break             inches, the two combined
  release_height          feet
  release_side            feet

A right-hander and a left-hander mirror each other on the clock: a RHP four-seam
near 1:16 and a LHP four-seam near 10:40 are the same pitch. This staff sits
exactly there. Because throws is missing for most of the roster, read the axis
relative to that pitcher's OWN fastball rather than against an absolute target
whenever you are unsure of his handedness.

Bauer Units = spin_rate divided by velocity. Use it whenever you judge spin,
because raw rpm punishes a high school arm for throwing 78 rather than 93. MLB
averages 23.9; this staff runs 21 to 27 on the fastball. A pitcher here can be
average in Bauer Units and well below average in raw spin, and the Bauer number is
the fairer read.

WHAT EACH PITCH SHOULD LOOK LIKE:

  4-seam     Spin efficiency 95-100, axis 12:30-1:30 for a RHP. More true spin and
             a higher axis produce more vertical break, which is what plays at the
             top of the zone and misses bats. MLB averages 20 inches of vertical
             break; this staff averages about 15, so treat 20 as the direction of
             travel and not as a pass mark.
  2-seam     High efficiency and high spin with a more tilted axis than the
             four-seam, 1:30-2:30 for a RHP. More tilt buys run and costs ride.
             A lively one carries 10 to 13 inches of ride with 18 or more of run;
             a heavy one carries less ride still. Some of that run and sink comes
             from how the seams meet the air rather than from spin, which is why
             a two-seam can out-move its own axis. Rapsodo does not separate the
             two, so never claim a seam effect: if his two-seam runs more than
             its axis suggests, say that and leave the why to the coach.
  Cutter     Efficiency 45-65, below the fastball but above a slider. 6 to 10
             inches of vertical break with horizontal break between -3 and +3.
             Should sit within 5 mph of the fastball. Above the band it flattens
             back into a fastball; below it, it blends into a slider and loses
             the spin-induced break that makes it a cutter.
  Gyro       Efficiency under 10. Axis 11:00-12:00 for a RHP. Almost no
  slider     spin-induced movement, under 1 to 2 inches in both directions, its
             depth coming from gravity rather than spin. It can be thrown harder
             than a sweeper and is the slider that pairs with a four-seam.
  Sweeping   Efficiency 15-40, spin 2400-2500, axis 10:00-12:00 for a RHP, with
  slider     horizontal break of -5 to -10 or more. More efficient than a gyro
             slider, less than a curveball. Pairs with a two-seam or a changeup.
             A slider above 40 percent efficiency is no longer a slider: it has
             become a slurve or a loopy curve. Three slider faults show in the
             numbers before they show on video: efficiency drifting over 35 to
             40, a velocity gap of more than 10 mph off the fastball, and an axis
             that wanders pitch to pitch, which costs him command before it costs
             him movement.
  12-6       Efficiency 75-90, axis close to 6:00. Aim for -15 inches or more of
  curveball  vertical break with little horizontal break.
  Loopy      2500+ rpm, axis 8:00-9:00, efficiency 60-80. Bigger movement and
  curveball  slower velocity than a 12-6.
  Slurve     Efficiency 50-65, axis 7:00-8:00. Horizontal break together with
             negative vertical break. Varies a lot with slot.
  Change-up  Lower spin than the fastball, usually 300+ rpm down, with a more
             tilted axis and high efficiency. At least 5 to 8 inches LESS vertical
             break than his fastball and MORE horizontal break than it. The axis
             should tilt at least a full hour, 30 degrees, off the fastball. The
             best ones show more horizontal than vertical break.
             If it will not tilt — axis within an hour of his fastball and ride
             that will not come down whatever the grip — and his fastball shows
             cut rather than run, he may be a supinator, and a conventional
             circle change is fighting his hand. The kick change, middle finger
             spiked so it leaves the ball last, is the modern option for that
             arm: it kills ride and adds run without asking him to pronate.
             Raise it as a question for the coach, never as a prescription.
  Splitter   Very low total spin from the wide grip. The less true spin, the more
             it tumbles. Velocity within 6 to 10 mph of the fastball.

FASTBALL SHAPE, from induced vertical break and the magnitude of horizontal break,
ignoring its sign:
  ride or carry   IVB 17 or more, horizontal break under 12
  ride-run        both 16 or more
  runner          horizontal break 16 or more, IVB 6 to 12
  sinker          horizontal break 16 or more, IVB 5 or less
  dead zone       IVB under 15 AND horizontal break under 15. Rapsodo's own
                  material calls the same thing the flat zone
A fastball's shape decides its job. High vertical break misses bats at the top of
the zone; cut or sink produces soft contact. Dead zone does neither, and it is the
most common shape on this staff. When you see it, say so and check Bauer Units,
because a dead zone fastball with healthy Bauer Units is a shape and axis problem
rather than a spin problem, which is a completely different conversation with a
pitcher. A four-seam with 25 or more inches of total break is a good one at any
level.

SEPARATION. Two pitches are effectively one pitch when both breaks are within
about an inch of each other AND the spin axis is within 5 degrees, which is ten
minutes on the clock. A changeup wants a full hour of tilt off the fastball, 30
degrees; a sinker at least half an hour. Always compare a pitch against that same
pitcher's other pitches, never against the staff.

EVERYTHING IS RELATIVE TO HIS FASTBALL. A secondary pitch is graded by its
distance from his own fastball — in velocity, in movement, in axis — more than by
its own numbers. A slider that sweeps 8 inches is a different pitch behind a
16-inch runner than behind a 17-inch rider.

MIRRORING. A fastball and a curveball whose axes sit about 180 degrees apart look
identical out of the hand and then break opposite ways; a slider and a changeup
can pair the same way. Add 180 to his fastball axis and compare it with his
curveball's: within about 10 degrees is a true mirror, and worth telling him he
has it. Read both axes from his own pitches.

COMMAND GATES ALL OF IT. A pitch he cannot land is not a weapon no matter how it
grades, so read game strike% alongside the shape. A beautifully shaped pitch thrown
for strikes 40 percent of the time is a project, not an out pitch.

Where throws is null, do not name a side. Give the magnitude of horizontal break
rather than calling it arm-side or glove-side.
</rapsodo_metrics>

<which_shape_should_he_chase>
Dead zone is a problem; the fix depends on where he releases the ball, not on
preference. Release height on this staff runs 5.1 to 6.7 feet, median 5.9.

  Releases high, roughly 6.2 feet or above
      He can support ride. Getting induced vertical break toward 17 and raising
      spin efficiency is a realistic direction.
  Releases low, roughly 5.5 feet or below
      Ride is largely unavailable to him and chasing it wastes a season. His path
      is run and sink: horizontal break toward 16 or more, a more tilted axis,
      accepting less vertical break.
  Releases in the middle
      An outlier shape in either direction is probably not available. The goal is
      simply to not sit in the dead zone at that slot — movement life in whichever
      direction his axis already leans.

Ride follows slot. A higher release supports more ride and a lower one less, so
judge his fastball's ride against what his own slot allows rather than against
the staff's best rider, and say so when he beats his slot — that is the arm to
protect. A fastball plays at the top of the zone by arriving flat, and flatness
comes from a low release as much as from ride, so a low-slot arm with modest
ride can still own the top of the zone; what he cannot do is chase ride he does
not have. We do not measure approach angle, so say this as a principle, never as
a number.

Never tell a low-slot pitcher his fastball needs more ride, and never tell a
high-slot pitcher to sink it. Read release_height before you say anything about
what his fastball should become.
</which_shape_should_he_chase>

<cues>
When the data points clearly at one fix, you may offer the matching cue in
"watch", as something to try rather than an instruction. At most one per note.
These are grip, seam and intent cues, which follow from numbers you can see. They
are not body mechanics, which you must never prescribe.

  Fastball needs more vertical break
      move the pointer and middle finger closer together; tuck the thumb; work
      the pitch at the top of the zone
  Fastball is cutting unintentionally
      move the fingers closer together; put the horseshoe on the pointer finger
      side for a four-seam
  Fastball needs more horizontal movement
      tilt the axis; keep the palm inside the ball; consider a two-seam
  Changeup lacks depth or separation
      keep the palm inside the ball through release; widen the grip; let it roll
      off the middle finger; hit the catcher's feet
  Curveball efficiency too low
      throw the front of it; turn it late; break it from the catcher's mask to
      the dirt; get the middle finger on a seam
  Slider efficiency too high for a gyro shape
      turn the door knob; throw the side of it; think fastball longer and fall
      off the side of it; think more velocity and less movement; throw it like
      a football spiral
  Slider needs more sweep
      come around the ball; three o'clock to nine o'clock; show the back of the
      hand to the catcher
  Changeup will not tilt and his fastball cuts
      the kick change: spike the middle finger so it is the last thing on the
      ball and let the ring finger kill the spin
  Building a cutter
      throw the arm side of the baseball; backspin a slider; aim for 45 to 65
      percent efficiency
</cues>

<plain_english>
He has probably never been taught these words, so carry the meaning with the
number every time. Use these translations:

  induced vertical break  ride or carry: how much the ball fights gravity on the
                          way in. More of it is what makes a fastball play at the
                          top of the zone. Negative is drop
  horizontal break        run or sweep: how far it moves sideways
  spin efficiency         how much of his spin actually moves the ball. The rest
                          is gyro, bullet spin, which gives a pitch depth instead
                          of movement — which is exactly what a slider wants and
                          a fastball does not
  spin axis               the tilt of the spin, read like a clock face
  total break             how much the pitch moves overall
  Bauer Units             spin per mph. It lets him compare his spin to a big
                          leaguer's without being punished for throwing slower
  release height          where he lets go of the ball. It sets the angle the
                          pitch comes in on, and it is a trait rather than a
                          virtue — neither tall nor short is the good one

Baseball shorthand he already knows is fine: ride, run, sink, sweep, depth, out
pitch, slot, get-me-over.
</plain_english>"""

# The note and the coach-facing pitching chat are different surfaces with
# different output contracts, but the pitch design framework above is true on
# both, so it is written once and shared. agent.py appends it to the system
# prompt when a conversation is scoped to the pitching side.

SYSTEM_PITCHING = """<role>
You are the pitching development analyst for Archbishop Moeller High School. The
staff can read a table; your job is the interpretation they would otherwise have
to work out themselves. Write about this pitcher specifically — what kind of arm
he is, what he is doing right now, and what it means — not a template with his
numbers dropped into it.
</role>

<what_you_are_given>
A compact object the database has already computed. You never see raw pitches and
must not ask for them. Fields that may appear:

  changes            shifts against his own baseline, already scoped to a pitch
                     type where that matters, with effect_size and the recent and
                     baseline counts behind them
  recent_sessions    his last 4 sessions. Bullpens carry Rapsodo measurements;
                     cage sessions carry Blast swing metrics. A two-way player
                     has both, and his cage work is not pitching evidence. A
                     session's numbers pool every pitch type thrown that day,
                     so they carry no spin axis and describe no single pitch
  bullpen_pitch_mix  what he has been throwing lately, and the shift
  stuff_by_pitch     THE ARSENAL. One entry per pitch he throws enough of, with
                     that pitch's own velocity, top velocity, spin rate, spin
                     efficiency, vertical break, horizontal break and release
                     height, each with a percentile against the arms he is
                     pooled with, plus that pitch's spin_axis in degrees with
                     its clock reading (unranked: an axis is only ever compared
                     with his own other pitches). This is the only place a
                     single pitch's shape or axis appears — recent_sessions
                     averages a bullpen across everything he threw that day.
                     Every judgement about shape, separation, mirroring and
                     whether a pitch looks like its own type comes from here.
                     Horizontal break is ranked as a distance, so a left-hander
                     is not penalised for a negative number. A pitch he has
                     thrown too few of is absent here, not zero — say it is
                     thin rather than treating it as missing from his arsenal
  game_pitching      per pitch: usage, avg velo, strike%, whiff%
  goals              active development goals and progress
  interventions      logged work, with before and after

An empty changes list is common and means nothing cleared the thresholds, not that
he is static. When it is empty, the sessions, the mix and the game splits are your
evidence.
</what_you_are_given>

""" + PITCHING_KNOWLEDGE + """

<how_to_read_a_pitcher>
This note is written so a coach can hand it straight to the pitcher, so it has to
tell him who he is before it tells him what is wrong. A sixteen year old who opens
on his deficit stops reading.

0. WHAT KIND OF ARM IS HE? Settle this first and say it first. Sinker-runner,
   ride guy, gyro-slider guy, three-pitch strike-thrower. Use his own numbers to
   say it — "he is a run-and-sink arm, 16.7 inches of horizontal break" — and say
   it as a real identity rather than a consolation. Dead zone means he does not
   have one yet, and even that is better delivered as "his fastball has not picked
   a direction" than as a failure.

Then work in this order. The first thing that is true and matters becomes the
second half of the "read".

1. What shape is his fastball? Name the shape. Dead zone outranks almost anything
   else you could say about him. If it is dead zone, check release_height before
   saying which way he should go.
2. Does the arsenal separate? Run the separation test against his own fastball.
   When two pitches fail it, say so plainly and name both — the hitter sees one
   pitch out of the hand, and that is the most actionable thing you can tell a
   coach.
3. Do any two pitches mirror? Add 180 degrees to his fastball axis and compare it
   with his curveball's, or his slider's with his changeup's. A true mirror is a
   real weapon and he should be told he has one.
4. Is each pitch behaving like its own type? Compare it against the profile for
   that pitch. A pitch whose numbers do not match its label is a labelling or an
   execution question worth raising, not a failure.
5. Can he land it? Game strike% decides whether any of the above is usable.
6. Is the fastball trending? Velocity is the headline development metric for a
   high school arm. Say where it sits and which way it is moving.
7. Is the delivery repeating? release_height and release_side are properties of
   the whole delivery, not of any one pitch. A slot that moved changes every pitch
   downstream, so when it moves it outranks a single pitch finding.
8. Did it play? Whiff% is whether it misses bats. A pitch that grades well in the
   pen and gets hit, or the reverse, is the more interesting story, and setting
   training against games is the whole reason they live in one system. A sink-
   and-run fastball is judged on strikes and contact, not on whiffs.
9. Is he practicing what he pitches? Compare bullpen_pitch_mix with game_pitching
   usage.
10. Is the plan working? Goals and interventions, and whether the numbers moved.

Stop when the evidence runs out. Most pitchers here will not support all ten.
</how_to_read_a_pitcher>

<output>
Return JSON matching the schema you are given. Each field renders differently on
the page, so keep every field doing its own job.

read      TWO clauses in one sentence: what kind of arm he is, then the thing that
          matters most right now. Identity first, always. "He is a run-and-sink
          arm with 16.7 inches of horizontal break, and the next step is a
          changeup that separates from it" beats "IVB 14.7, horizontal break
          12.1". Never open on the deficit.
overview  THE PART HE ACTUALLY READS. Three to five sentences of plain English
          that stand on their own, written so a pitcher who reads only this
          paragraph knows what kind of arm he is, what is working, what is
          holding him back and what he is doing about it next. Everything below
          is the evidence for this; this is the conclusion.

          Write it in this order: what he is, what is already good, the one
          thing in the way, and what he does next. THE LAST SENTENCE IS ALWAYS
          THE NEXT STEP — never end this paragraph on a description, however
          interesting, because the last line is the one he leaves with.

          Carry THREE numbers at most in the whole paragraph, and only ones that
          decide something. This is the story; findings is where the arithmetic
          lives. If you find yourself listing a pitch's measurements here, they
          belong below instead. Name a pitch in words a pitcher uses: "your
          slider" rather than "SL", "how much your fastball rides" rather than
          "induced vertical break".

          Say it straight without being brutal. "Your changeup and your fastball
          are moving almost the same way, so a hitter sees one pitch out of your
          hand" is honest and useful; "your changeup is bad" is neither. He
          should finish it knowing exactly where he stands and what to go do.
findings  2 to 5 groups, PARENT then items. The parent is a pitch code — FB SI CT
          SL CB CH SP — when the items are that pitch's variables, including its
          game strike and whiff rates; DELIVERY for release height, release side
          and slot; MIX for bullpen usage shifts; GAME for results not tied to one
          pitch. Never repeat a parent. One short sentence per item, each resting
          on a number you were given, with the number shown. Set each item's tone
          to good, bad or neutral. Four things govern these items:

          LEAD WITH WHAT IS WORKING. The first group is something he is doing
          well, whenever the data supports one, and it has to be specific and
          earned — "he spins his fastball at 26.5 Bauer Units, which is above the
          big league average even at 76 mph" is worth more to him than any
          criticism. He needs to know what not to break.

          TRANSLATE EVERY NUMBER. Assume he has never been taught what these
          metrics are. A number is only finished when it carries its "so what":
          not "14.9 inches of induced vertical break" but "14.9 inches of ride,
          which is what makes a fastball play at the top of the zone". Never
          leave a metric name standing on its own.

          SAY WHETHER HE IS GETTING BETTER. When changes carries a trend, give it
          plainly and with the timeframe — "his fastball is up 2.3 mph since
          February". This is the single line a pitcher most wants, so do not bury
          it below the shape analysis.

          SAY WHERE HE STANDS. stuff_by_pitch gives a percentile against the arms
          he is pooled with. Use it, name the pool — "67th percentile spin among
          the varsity arms" — and never name another player. Release height is
          deliberately unranked: it is a trait, not a virtue, so report it and
          never call either end of it good.
watch     Exactly one thing for the pitcher to DO, then at most one thing to
          bring to his coach.

          The action is concrete, specific and doable inside a week: "next
          bullpen, throw ten changeups trying to hit the catcher's feet" rather
          than "worth monitoring the changeup". When the data points clearly at
          one, this is where a grip, seam or intent cue from the cue list goes.

          The second item, when there is one, is the question worth taking to a
          coach — "ask Coach whether a two-seam fits your slot". It keeps the
          conversation with the staff rather than replacing it, so phrase it as
          a question rather than a conclusion.

          Return an empty list if the data is too thin to support an honest
          action, and say so in caveat, rather than inventing one.
caveat    One sentence on sample size or thin data when a coach needs the warning,
          or "" when there is nothing to flag. Saying that something IS solid
          belongs here too.
</output>

<rules>
Every claim rests on a number you were given. If it is not in the context, do not
mention it.
A change is a comparison, not a cause. You may note that an intervention was logged
near a change; you may not say it caused it.
Name the pitch — "his fastball's horizontal break", never "his horizontal break" —
and never generalise one pitch's number to the whole arsenal.
What he is throwing is a finding in itself. A notable bullpen mix shift is a
deliberate act; report it as a change in usage, never as a change in the pitch.
A runner or sinker arm is not supposed to miss bats with the fastball. Read that
fastball's game results by strike% and, when we have it, contact quality, and
never call a low fastball whiff% a fault on a sink-and-run profile.
Ride moves with where he was throwing. A fastball worked low shows more ride than
the same fastball worked up, so before calling a ride change real, ask whether the
bullpen was working a different part of the zone. When you cannot tell, say so.
Do not prescribe body mechanics. You have no video and no biomechanics, so never
"lower his arm slot", "shorten his stride" or anything about how he moves. You MAY
offer one grip, seam or intent cue from the cue list, because those follow from
numbers you can see — always as an option to try, never as an instruction.
Plain text inside every string, no markdown and no bullets. Baseball shorthand is
fine. Write for coaches about him, never to him.
Before you finish, check that every number you used appears in the context and that
no parent is repeated.
</rules>"""



SYSTEM_HITTING = """You are the hitting development analyst for Archbishop Moeller \
High School. The coaching staff sees the numbers themselves; your job is to read \
them the way a sharp analyst would and tell the staff what they mean, so they \
don't have to work it out from a table.

You are given a compact summary that the database has already computed: recent \
cage sessions, detected changes against the hitter's own baseline, what kind of \
swings he has been taking, his game results at the plate, where he sits against \
Blast's published bands for his level, active development goals, and any logged \
interventions. You are NOT given the raw swings, and you must not ask for them.

BE AN ANALYST, NOT A REPORT. Anyone can list what moved; what earns your place \
is the interpretation. You answer as JSON matching the schema you are given, \
and the page renders each field with its own visual treatment, so keep every \
field doing exactly its own job:

- "read": ONE sentence, the single thing that matters most right now and why. \
The interpretation, not the numbers -- "He has moved almost entirely off the \
tee and onto live reps, and the bat has held up" beats "tee share fell 74.9 \
points". A coach who reads only this line should know what to think about.
- "findings": grouped PARENT -> CHILDREN, 2 to 5 groups. The parent names what \
the evidence is about; the items are its variables, ONE short sentence each, \
each resting on a number you were given (show the number). Parents: "SWING" \
for the engine -- bat speed, hand speed, rotational acceleration, power; \
"PATH" for where the barrel goes -- attack angle, on-plane efficiency, \
vertical bat angle; "CONTACT" for timing and the body-to-barrel relationship \
-- time to contact, commit time, early connection, connection at impact; \
"DRILLS" for a shift in what kind of swings he is taking; "BENCHMARK" for \
where he sits against Blast's published band for his level; "GAME" for results \
at the plate. Never repeat a parent. Order groups by how much a coach should \
care. ALWAYS use the game data (game_hitting) when it is present -- cage work \
says what the swing is doing, game data says whether it played, and setting the \
two against each other is the whole reason they live in one system. Set each \
item's "tone" to "good" for a favorable development, "bad" for a concerning \
one, "neutral" for information that is neither.
- "watch": 1 or 2 suggestions the staff could act on, each opened with "Worth" \
or "Suggest" so it reads as an option, not an instruction. Things to CHECK, \
ASK, MEASURE or WATCH, and conditionals tied to what the data would show: \
"Worth asking whether the move off the tee was planned -- if it was, his live \
bat speed is the number to track from here." Do NOT prescribe mechanics: you \
do not see him swing, have no video and no biomechanics, so never "get on plane \
earlier", "stay through the ball", "close his stance", or any instruction about \
how to move his body. The staff decides what to do; you point at what deserves \
attention. If the data is too thin to suggest anything useful, return an empty \
list and say so in "caveat" -- never invent a suggestion.
- "caveat": one sentence on sample size or data thinness when a coach needs the \
warning ("two tagged sessions is thin for calling a path change settled"), or \
"" when there is nothing to flag. Saying something IS solid also belongs here.

Rules for every field:
- Every claim rests on a number you were given. Never invent one; if something \
isn't in the context, don't mention it.
- A change is a comparison, not a cause. If an intervention is logged near a \
change you may note the timing, but do not claim the intervention caused it.
- UP IS NOT AUTOMATICALLY GOOD. Attack angle, vertical bat angle, early \
connection and connection at impact are TARGET BANDS: a hitter whose attack \
angle climbs from 11 to 19 degrees has got worse, not better. Every change you \
are given already carries a "favorable" flag computed from the registry -- \
trust that flag over the direction of the number, always.
- NAME THE DRILL. Changes are scoped to one drill because a tee swing and a \
live swing are different measurements -- our hitters average about 2 mph slower \
off a tee. Say "his tee bat speed" or "his machine work", never "his bat \
speed", and never generalise one drill's number to his swing as a whole.
- WHAT KIND OF SWINGS HE TAKES IS A FINDING TOO. A notable cage_drill_mix shift \
is a deliberate act worth a coach's attention -- report it under DRILLS as a \
change in what he is practising, NEVER as a change in his swing. It fires no \
change detection by design, so if you don't report it nobody sees it.
- Untagged swings drive no finding. If much of his work is untagged, that is \
worth one line in "caveat" -- it is fixed by tagging in the Blast app.
- Blast's bands are the VENDOR's, not ours, and two of them (attack angle, \
vertical bat angle) are wide enough that every Moeller hitter clears them. \
Clearing those two is not evidence of anything; do not present it as a finding.
- An empty change list means nothing cleared the thresholds, not that he isn't \
improving. Say so plainly when that is the story.
- Plain text inside every string -- no markdown, no bullets. Baseball shorthand \
is fine. Do not address the player; you write for coaches about him."""


# One analyst per side. The pitching prompt asks for findings grouped under pitch
# codes and DELIVERY; a hitter has neither, and before this split every hitter's
# note was written by a model told it was the pitching analyst. That was
# harmless while hitters had no swing data and stopped being harmless the day
# 85,917 swings landed.
#
# A two-way player gets the analyst for what he PRIMARILY is (players.is_pitcher),
# because a note needs one voice. His other side still reaches the model -- the
# context carries both -- and each prompt can group it under GAME.

# What _call_model forces the note into. The template renders these fields
# directly, so the shape is a contract, not a suggestion.
# The parent enum is per side on purpose: a pitching note must not be able to
# emit "SWING", and a hitting note must not be able to emit "FB". The enum is the
# only thing that actually enforces it -- the prompt asks, the schema guarantees.
PITCHING_PARENTS = ["FB", "SI", "CT", "SL", "CB", "CH", "SP",
                    "DELIVERY", "MIX", "GAME"]
HITTING_PARENTS = ["SWING", "PATH", "CONTACT", "DRILLS", "BENCHMARK", "GAME"]


def note_schema(parents, overview=False):
    """The note's shape. `overview` adds the plain-language synthesis field.

    Opt-in rather than always-on because it is required once present, and
    SYSTEM_HITTING is still the placeholder prompt -- adding a required field
    it has never been told to write would make every hitting note fail. Turn it
    on there in the same change that rewrites that prompt.
    """
    required = ["read", "findings", "watch", "caveat"]
    if overview:
        required.insert(1, "overview")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": {
            "read": {"type": "string"},
            **({"overview": {"type": "string"}} if overview else {}),
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["parent", "items"],
                    "properties": {
                        "parent": {"type": "string", "enum": list(parents)},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["text", "tone"],
                                "properties": {
                                    "text": {"type": "string"},
                                    "tone": {"type": "string",
                                             "enum": ["good", "bad", "neutral"]},
                                },
                            },
                        },
                    },
                },
            },
            "watch": {"type": "array", "items": {"type": "string"}},
            "caveat": {"type": "string"},
        },
        }


PITCHING_NOTE_SCHEMA = note_schema(PITCHING_PARENTS, overview=True)
HITTING_NOTE_SCHEMA = note_schema(HITTING_PARENTS)


def note_spec(context):
    """(system prompt, response schema) for whichever analyst this player needs.

    Keyed off context["role"], which build_context already sets from
    players.is_pitcher -- so this needs no new argument threaded through
    generate() or the injectable call_model stub the tests use.
    """
    if (context or {}).get("role") == "pitcher":
        return SYSTEM_PITCHING, PITCHING_NOTE_SCHEMA
    return SYSTEM_HITTING, HITTING_NOTE_SCHEMA


def parse_note(text):
    """The structured note dict if the stored summary is the JSON shape.

    Older cached notes are plain prose; callers fall back to rendering those as
    a paragraph, so this returns None rather than raising on them.
    """
    if not text or not text.lstrip().startswith("{"):
        return None
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not (isinstance(d, dict) and d.get("read")):
        return None
    # A note cached in an older shape (flat findings, no parent) falls back to
    # the plain-paragraph rendering instead of breaking the template.
    if any(not (isinstance(g, dict) and "parent" in g)
           for g in d.get("findings") or []):
        return None
    return d


# ---------------------------------------------------------------------------
# What the summary was written from
# ---------------------------------------------------------------------------

def basis(engine, player_id):
    """A hash of everything the summary depends on.

    If this is unchanged, the stored summary is still current and no model call
    is needed. If any of it moves -- a new session, a new detected change, a new
    goal -- the hash changes and the summary is regenerated on the next run.
    """
    with engine.connect() as conn:
        latest = conn.execute(
            select(func.max(db.sessions.c.id))
            .where(db.sessions.c.player_id == player_id)).scalar()
        change_ids = [r[0] for r in conn.execute(
            select(db.change_events.c.id)
            .where(db.change_events.c.player_id == player_id)
            .order_by(db.change_events.c.id))]
        goal_ids = [f"{r.id}:{r.status}" for r in conn.execute(
            select(db.goals.c.id, db.goals.c.status)
            .where(db.goals.c.player_id == player_id)
            .order_by(db.goals.c.id))]
        iv_ids = [f"{r.id}:{r.outcome}" for r in conn.execute(
            select(db.interventions.c.id, db.interventions.c.outcome)
            .where(db.interventions.c.player_id == player_id)
            .order_by(db.interventions.c.id))]
    raw = f"{latest}|{','.join(map(str, change_ids))}|{','.join(goal_ids)}|{','.join(iv_ids)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# A pitch's share of the work moving by this much is worth a coach's attention.
# Below it, it's just the normal shape of a bullpen.
MIX_SHIFT_PP = 8.0


def training_pitch_mix(engine, player_id):
    """What he actually threw recently vs before, as usage percentages.

    Usage is a development fact in its own right -- a pitcher going from 7% to 28%
    sliders is doing something different on purpose. It is deliberately NOT a
    change in any pitch, and change detection now compares within a pitch type so
    a mix shift can no longer masquerade as one (see metrics.PITCH_SPECIFIC).
    Computed here so the model is handed the comparison rather than doing
    arithmetic on raw counts.
    """
    with engine.connect() as conn:
        dates = [r[0] for r in conn.execute(
            select(db.sessions.c.session_date).distinct()
            .where((db.sessions.c.player_id == player_id) &
                   (db.sessions.c.source == "rapsodo"))
            .order_by(db.sessions.c.session_date.desc()))]
        if len(dates) < metrics.RECENT_SESSIONS + 1:
            return None
        recent, baseline = dates[:metrics.RECENT_SESSIONS], dates[metrics.RECENT_SESSIONS:]

        def counts(window):
            rows = conn.execute(
                select(db.pitch_metrics.c.pitch_type, func.count())
                .select_from(db.pitch_metrics.join(
                    db.sessions, db.sessions.c.id == db.pitch_metrics.c.session_id))
                .where((db.pitch_metrics.c.player_id == player_id) &
                       (db.pitch_metrics.c.metric_key == "velocity") &
                       (db.sessions.c.session_date.in_(window)))
                .group_by(db.pitch_metrics.c.pitch_type)).all()
            total = sum(n for _pt, n in rows) or 1
            return {(pt or "unlabelled"): round(100.0 * n / total, 1) for pt, n in rows}

        # Inside the connection block -- counts() closes over `conn`.
        now, before = counts(recent), counts(baseline)
    shifts = []
    for pt in sorted(set(now) | set(before)):
        a, b = before.get(pt, 0.0), now.get(pt, 0.0)
        if abs(b - a) >= MIX_SHIFT_PP:
            label = metrics.PITCH_TYPE_LABELS.get(pt, pt)
            shifts.append(f"{label} {a}% -> {b}% of his pitches "
                          f"({'+' if b > a else ''}{round(b - a, 1)} pts)")
    return {
        "recent_window": f"last {len(recent)} bullpens",
        "recent": {metrics.PITCH_TYPE_LABELS.get(k, k): f"{v}%" for k, v in now.items()},
        "baseline": {metrics.PITCH_TYPE_LABELS.get(k, k): f"{v}%" for k, v in before.items()},
        "notable_shifts": shifts,
    }


def training_drill_mix(engine, player_id):
    """What kind of swings he actually took recently vs before, as percentages.

    The hitting twin of training_pitch_mix, and it earns its place the same way:
    a change in what a hitter PRACTISES is a finding in its own right, and it is
    invisible to change detection by design. Because every Blast metric is
    compared within its own drill, a hitter who moves from 80% tee work to 80%
    live reps fires nothing at all -- correctly, since none of his drills changed
    -- and yet he is plainly doing something different, and a coach should know.

    Reported separately from swing changes and never merged with them. "He is
    taking live reps now instead of tee work" and "his tee bat speed is up" are
    two different statements, and only the second is about his swing.
    """
    with engine.connect() as conn:
        dates = [r[0] for r in conn.execute(
            select(db.sessions.c.session_date).distinct()
            .where((db.sessions.c.player_id == player_id) &
                   (db.sessions.c.source.in_(("blast", "hittrax"))))
            .order_by(db.sessions.c.session_date.desc()))]
        if len(dates) < metrics.RECENT_SESSIONS + 1:
            return None
        recent, baseline = dates[:metrics.RECENT_SESSIONS], dates[metrics.RECENT_SESSIONS:]

        def counts(window):
            rows = conn.execute(
                select(db.swings.c.context, func.count())
                .select_from(db.swings.join(
                    db.sessions, db.sessions.c.id == db.swings.c.session_id))
                .where((db.swings.c.player_id == player_id) &
                       (db.swings.c.metric_key == "bat_speed") &
                       (db.sessions.c.session_date.in_(window)))
                .group_by(db.swings.c.context)).all()
            total = sum(n for _c, n in rows) or 1
            return {(c or "untagged"): round(100.0 * n / total, 1) for c, n in rows}

        # Inside the connection block -- counts() closes over `conn`.
        now, before = counts(recent), counts(baseline)

    label = lambda c: "Untagged" if c == "untagged" else metrics.split_label(c)
    shifts = []
    for c in sorted(set(now) | set(before)):
        a, b = before.get(c, 0.0), now.get(c, 0.0)
        if abs(b - a) >= MIX_SHIFT_PP:
            shifts.append(f"{label(c)} {a}% -> {b}% of his swings "
                          f"({'+' if b > a else ''}{round(b - a, 1)} pts)")
    return {
        "recent_window": f"last {len(recent)} sessions",
        "recent": {label(k): f"{v}%" for k, v in now.items()},
        "baseline": {label(k): f"{v}%" for k, v in before.items()},
        "notable_shifts": shifts,
        "note": ("Drill usage, NOT a swing change. Every Blast metric is compared "
                 "within one drill, so a shift in the mix fires no finding -- which "
                 "is why it is reported here instead."),
    }


def build_context(engine, player_id):
    """The compact context the model is given. No raw rows, ever.

    A pitcher with 2,400 tracked pitches produces a few hundred tokens here --
    that is the whole point of roadmap section 9.
    """
    import profiles
    with engine.connect() as conn:
        p = conn.execute(select(db.players)
                         .where(db.players.c.id == player_id)).first()
        if not p:
            return None
    prof = profiles.profile(engine, p.slug)

    ctx = {
        "player": prof["player"]["name"],
        "role": "pitcher" if prof["player"]["is_pitcher"] else "position player",
        "bats": prof["player"]["bats"], "throws": prof["player"]["throws"],
        "last_session": prof["last_session"],
        "training_sessions": len(prof["training"]),
    }

    # Only unacknowledged changes -- acknowledged ones are already old news.
    ctx["changes"] = [
        {"summary": c["summary"], "severity": c["severity"],
         "favorable": c["favorable"], "detected_on": c["detected_on"],
         "effect_size": c["effect_size"],
         "observations": f"{c['n_recent']} recent vs {c['n_baseline']} baseline"}
        for c in prof["changes"] if not c["acknowledged"]][:6]

    ctx["recent_sessions"] = [
        {"date": s["date"], "type": s["type"], "purpose": s["purpose"],
         # spin_axis is left out: a session pools every pitch type thrown
         # that day, and the pooled figure is an ARITHMETIC mean of a circular
         # quantity -- 355 and 5 average to 180. Per-pitch axes live in
         # stuff_by_pitch, circular-meaned, which is the only honest version.
         "metrics": {k: f"{m['mean']}{m['unit']} (n={m['n']})"
                     for k, m in s["metrics"].items() if k != "spin_axis"}}
        for s in prof["training"][:4]]

    ctx["goals"] = [
        {"title": g["title"], "status": g["status"],
         "metric": g["metric_label"], "target": g["target_value"],
         "progress": g["progress"].get("state"),
         "current": g["progress"].get("current"),
         "pct_of_the_way": g["progress"].get("pct")}
        for g in prof["goals"] if g["status"] == "active"][:5]

    ctx["interventions"] = [
        {"title": i["title"], "date": i["date"], "category": i["category"],
         "outcome": i["outcome"],
         "before_after": [f"{m['label']} {m['pre_mean']}->{m['post_mean']}{m['unit']}"
                          for m in (i.get("evaluation") or [])[:3]]}
        for i in prof["interventions"][:4]]

    if prof["player"]["is_pitcher"]:
        mix = training_pitch_mix(engine, player_id)
        if mix:
            ctx["bullpen_pitch_mix"] = mix
        # Per-pitch shape AND where it ranks, in one object. recent_sessions
        # averages a bullpen across everything he threw that day, which blends
        # a fastball and a curveball into an induced vertical break that
        # describes neither -- so every arsenal judgement (shape, separation,
        # does this pitch look like its own type) has to come from here.
        # percentiles already pools by level and ranks horizontal break as a
        # magnitude, so a left-hander is not punished for a negative number.
        try:
            import percentiles as _pct
            strips = _pct.by_pitch(engine, player_id)
        except Exception:
            strips = []
        if strips:
            ctx["stuff_by_pitch"] = [
                {"pitch": s["pitch"],
                 "pitches_measured": s["n"],
                 "ranked_against": f"{s['pool']} ({s['pool_n']} arms)",
                 "metrics": {
                     b["label"]: (f"{b['display']}{b['unit']}"
                                  + (f", {b['ord']} percentile" if b.get("ord")
                                     else " (a trait, not ranked)"))
                     for b in s["bars"]},
                 # The pitch's own axis, circular-meaned. Every published axis
                 # target is in clock time and the column is in degrees, so
                 # both are given. Unranked on purpose.
                 **({"spin_axis": f"{s['axis']} degrees, {s['axis_clock']} on the "
                                  f"clock (compare only with his other pitches)"}
                    if s.get("axis") is not None else {})}
                for s in strips]
    # Not an else: a two-way player swings as well as throws, and his cage work is
    # as much a part of his development picture as his bullpens.
    drills = training_drill_mix(engine, player_id)
    if drills:
        ctx["cage_drill_mix"] = drills

    # Where he sits against Blast's OWN bands. The hitting prompt has a BENCHMARK
    # group and is told two of these bands pass everybody, so it needs the flags
    # as well as the verdicts -- otherwise it reports clearing a wide band as a
    # finding. Only the misses and the near-misses are sent: an in-band metric
    # with nothing interesting about it is context the model does not need.
    try:
        import percentiles
        bench = percentiles.blast_benchmark(engine, player_id)
    except Exception:
        bench = None
    if bench:
        notable = [{"metric": b["label"], "value": b["display"] + b["unit"],
                    "band": b["range_txt"], "verdict": b["tone"],
                    "band_is_wide": b["wide"]}
                   for b in bench["bars"] if b["tone"] != "in"]
        ctx["blast_benchmark"] = {
            "level": bench["level_label"],
            "source": "Blast's published bands for his level, not Moeller data",
            "outside_the_band": notable or "nothing outside his band",
        }
        if bench.get("drill_note"):
            ctx["blast_benchmark"]["caveat"] = bench["drill_note"]

    game = prof.get("game") or {}
    if game.get("pitching"):
        g = game["pitching"]
        ctx["game_pitching"] = {
            "tracked_pitches": g["pitches"], "seasons": g["years"],
            "strike_pct": g["strike_pct"], "whiff_pct": g["whiff_pct"],
            # Per pitch, so game results can sit under the pitch they belong to.
            # "code" is our canonical pitch code; charted "Breaking Ball" has
            # none and keeps its raw name rather than borrowing a pitch.
            "by_pitch": [
                {"pitch": t.get("code") or t["pitch_type"], "n": t["n"],
                 "usage_pct": t["usage_pct"], "avg_velo": t["avg_velo"],
                 "strike_pct": t["strike_pct"], "whiff_pct": t["whiff_pct"]}
                for t in (g.get("pitch_types") or [])[:5]]}
    if game.get("batting"):
        b = game["batting"]
        ctx["game_hitting"] = {"pitches_seen": b["pitches_seen"],
                               "whiff_pct": b["whiff_pct"]}
    return ctx


def has_anything_to_say(ctx):
    """Don't spend a call on a player we know nothing about."""
    if not ctx:
        return False
    return bool(ctx.get("changes") or ctx.get("recent_sessions") or
                ctx.get("goals") or ctx.get("interventions"))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def cached(engine, player_id, current_basis=None):
    """The stored summary IF it is still current. None means regenerate."""
    want = current_basis or basis(engine, player_id)
    with engine.connect() as conn:
        row = conn.execute(
            select(db.ai_summaries)
            .where((db.ai_summaries.c.player_id == player_id) &
                   (db.ai_summaries.c.basis == want))).first()
    if not row:
        return None
    return {"summary": row.summary, "model": row.model,
            "created_at": str(row.created_at), "basis": row.basis}


def store(engine, player_id, want, text, model=MODEL):
    with engine.begin() as conn:
        # One row per player -- an out-of-date summary is worthless, not history.
        conn.execute(delete(db.ai_summaries)
                     .where(db.ai_summaries.c.player_id == player_id))
        conn.execute(insert(db.ai_summaries).values(
            player_id=player_id, basis=want, summary=text, model=model))


def _call_model(context):
    """The one place this module spends money."""
    system_prompt, schema = note_spec(context)
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        # The model thinks by default and max_tokens caps thinking AND text
        # together, so this is a THINKING budget plus a note, not a note length.
        # 1100 risked truncating the JSON; 2000 then broke outright once the
        # pitching prompt started asking for real analysis -- classifying shape,
        # running the separation test, checking the release-height gate. A
        # measured Homoelle call spent 1319 tokens thinking and was cut off mid
        # JSON at exactly 2000, which surfaces as "model returned nothing"
        # because the truncation guard below refuses to cache a partial note.
        # 6000 leaves room for the thinking the analysis actually needs. Still
        # small money: once per player per week, only when their data has moved.
        max_tokens=6000,
        output_config={"effort": "medium",
                       # The schema guarantees the reply parses; the template
                       # renders the fields directly.
                       "format": {"type": "json_schema", "schema": schema}},
        system=[{"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user",
                   "content": "Write the development note for this player.\n\n"
                              + json.dumps(context, indent=1, default=str)}],
    )
    if resp.stop_reason == "max_tokens":
        return ""   # never cache a truncated note
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def generate(engine, player_id, force=False, call_model=None):
    """Return a current summary, calling the model only if one is needed."""
    want = basis(engine, player_id)
    if not force:
        hit = cached(engine, player_id, want)
        if hit:
            return {**hit, "cached": True}

    ctx = build_context(engine, player_id)
    if not has_anything_to_say(ctx):
        return {"summary": None, "cached": False,
                "skipped": "no training data, changes, goals or interventions yet"}

    if call_model is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {"summary": None, "cached": False,
                    "skipped": "no ANTHROPIC_API_KEY on the server"}
        call_model = _call_model

    text = call_model(ctx)
    if not text:
        return {"summary": None, "cached": False, "skipped": "model returned nothing"}
    store(engine, player_id, want, text)
    return {"summary": text, "model": MODEL, "basis": want, "cached": False}


# ---------------------------------------------------------------------------
# The weekly job  (spec 9.3)
# ---------------------------------------------------------------------------

def players_with_new_data(engine, since):
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(
            select(db.sessions.c.player_id).distinct()
            .where(db.sessions.c.session_date >= since))]


def run_weekly(engine, days=7, everyone=False, call_model=None, dry_run=False,
               on=None):
    """For each player with new data this week, refresh the cached summary.

    Coaches then read stored text; nobody pays per view.
    """
    today = on or date.today()
    if everyone:
        with engine.connect() as conn:
            ids = [r[0] for r in conn.execute(
                select(db.players.c.id)
                .where(db.players.c.is_active == True))]  # noqa: E712
    else:
        ids = players_with_new_data(engine, today - timedelta(days=days))

    out = {"considered": len(ids), "written": 0, "cached": 0, "skipped": 0,
           "results": []}
    for pid in ids:
        if dry_run:
            ctx = build_context(engine, pid)
            out["results"].append({"player_id": pid,
                                   "would_write": has_anything_to_say(ctx),
                                   "context": ctx})
            continue
        res = generate(engine, pid, call_model=call_model)
        if res.get("cached"):
            out["cached"] += 1
        elif res.get("summary"):
            out["written"] += 1
        else:
            out["skipped"] += 1
        out["results"].append({"player_id": pid, **res})
    return out


def main(argv):
    engine = db.get_engine()
    dry = "--dry-run" in argv
    everyone = "--all" in argv
    pid = None
    if "--player" in argv:
        pid = int(argv[argv.index("--player") + 1])

    if pid:
        res = generate(engine, pid) if not dry else {
            "context": build_context(engine, pid)}
        print(json.dumps(res, indent=2, default=str))
        return

    print("\nWeekly player summaries" + ("  (dry run -- no API calls)" if dry else ""))
    res = run_weekly(engine, everyone=everyone, dry_run=dry)
    print(f"  considered {res['considered']} player(s)")
    if dry:
        for r in res["results"]:
            print(f"    #{r['player_id']:<4} would_write={r['would_write']}")
    else:
        print(f"    written  {res['written']}")
        print(f"    cached   {res['cached']}  (no API call needed)")
        print(f"    skipped  {res['skipped']}")
        for r in res["results"]:
            if r.get("summary") and not r.get("cached"):
                print(f"\n  #{r['player_id']}: {r['summary']}")
    print()


if __name__ == "__main__":
    main(sys.argv[1:])


# ---------------------------------------------------------------------------
# Group development notes -- one for the pitching staff, one for the hitters.
# Same rule as everywhere else: the database computes, the model explains. The
# context below is entirely pre-computed; the model reads across it and says
# what a coordinator should be thinking about this week.
# ---------------------------------------------------------------------------

def _group_players(engine, side):
    """(with_data, without_data) names for one side of the roster."""
    import profiles
    want_pitcher = side == "pitching"
    roster = [p for p in profiles.roster(engine)
              if bool(p["is_pitcher"]) == want_pitcher]
    return ([p for p in roster if p["sessions"]],
            [p for p in roster if not p["sessions"]])


def build_group_context(engine, side):
    """Everything the group note is written from. Pre-computed, compact."""
    import profiles
    with_data, without = _group_players(engine, side)
    ov = profiles.team_overview(engine)
    names = {p["name"] for p in with_data} | {p["name"] for p in without}

    ctx = {
        "group": "pitching staff" if side == "pitching" else "hitting group",
        "players_total": len(with_data) + len(without),
        "players_with_training_data": len(with_data),
        "players_without_any_training_data": len(without),
        # Only this side's changes -- the roster-wide feed mixes both.
        "recent_changes": [
            {"player": c["player"], "summary": c["summary"],
             "severity": c["severity"], "favorable": c["favorable"],
             "detected_on": c["detected_on"]}
            for c in ov["changes"] if c["player"] in names][:8],
        "active_goals_program_wide": ov["counts"]["active_goals"],
    }

    if side == "pitching":
        try:
            import season
            prog = season.program_development()
            ctx["program_benchmarks"] = {
                "by_class": [{"class": b["cls"], "pitchers": b["n"],
                              "median_fb_velo": b["fb_velo"],
                              "range": [b["fb_lo"], b["fb_hi"]]}
                             for b in prog["benchmarks"] if b["n"]],
                "season_medians": prog["line"],
                "year_over_year": [{"player": r["name"], "seasons": r["pair"],
                                    "fb_velo": [r["velo_from"], r["velo_to"]],
                                    "delta": r["delta"]}
                                   for r in prog["yoy"][:6]],
            }
        except Exception:                                   # noqa: BLE001
            pass
        try:
            import rapsodo_card
            cards = rapsodo_card.roster_cards(engine)
            by_id = {p["id"]: p for p in with_data}
            arms = [{"player": by_id[pid]["name"], "fb_velo": c["fb"],
                     "fb_max": c["fb_max"], "arm_slot": c["slot"],
                     "pitches_tracked": c["total"],
                     "mix": {m["pt"]: str(m["pct"]) + "%" for m in c["mix"]}}
                    for pid, c in cards.items()
                    if pid in by_id and c.get("fb") is not None]
            arms.sort(key=lambda a: -(a["fb_velo"] or 0))
            ctx["bullpen_arsenals"] = arms[:12]
        except Exception:                                   # noqa: BLE001
            pass
    else:
        ctx["note"] = ("Blast and HitTrax pipelines are built but no swing data "
                       "has landed yet, so hitting development is game data only "
                       "until the first export arrives.")
    return ctx
