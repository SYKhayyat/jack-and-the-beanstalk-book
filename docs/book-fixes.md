# Book fixes (from the downloaded PDF review)

PDF page = story page + 1 (cover is page 1). Plate N = story page N.

Status rule: FIXED only when a freshly rendered page shows it. A seed or
staging edit with no render behind it is NOT TESTED.

| # | PDF page | Defect | Decision | Status |
|---|---|---|---|---|
| 1 | cover | lines on cover | gradient image, not stacked strips | FIXED: cover PDF rasterised, smooth shade, no lines (2026-11-10) |
| 2 | 3 | 4 beans | beans in one row ON the palm; floating beans banned (first render drew a row hanging in the air) | FIXED in story p2 render (2026-11-10) |
| 3 | 4 | mother a floating torso, too big | `window_and_door` staging, waist-up in window | FIXED in story p3 render (2026-11-10) |
| 4 | 5 | generic main character | plate-edit cast line called every girl "he/his"; now uses the star's own pronouns | pronoun bug fixed; likeness NOT TESTED |
| 5 | 7 | walks away from castle | `toward_the_castle` staging, side view facing left | FIXED in story p6 render (2026-11-10) |
| 6 | 8,9,12,13,14 | giant standing / under table / looks at child | the chair faces the camera, so he sits in it facing the viewer, chin on chest; the child is ~0.21 of the frame (head below the seat); standing giant on story p13 is cut off by the top edge (`slot_line_cut`) | FIXED in test renders of story pages 7, 8, 11, 12, 13 (2026-11-10) |
| 7 | 10,11 | different window frame/view | plate 9 patched with ImageMagick | FIXED: plates 9/10 match; story p9 render keeps the window (2026-11-10) |
| 8 | 14 | much darker for no reason | plate 13 brightened with ImageMagick | story p13 render (2026-11-10) is warmer and only a little darker than p12; owner to judge the look |
| 9 | 15 | runs away from beanstalk | pinned to the right, side view | FIXED in story p14 render (2026-11-10) |
| 10 | 16 | giant not on stalk | hands on stem, boots on leaves | FIXED in story p15 render (2026-11-10) |
| 11 | 17 | axe points down | new moment (owner-approved): axe drawn back over her shoulder between chops, chips on the grass; exactly one axe | FIXED in story p16 render (2026-11-10): one axe, head up behind her head, chips on the grass, hen and harp by the door; the notch in the stalk did not show |
| 12 | 18 | giant falls wrong way | giant right of stalk, falls the same way | FIXED in story p17 render (2026-11-10) |
| 12b | 19 | text says "on the step", hen missing | hen dropped from text | FIXED: story p18 text checked (no step, no hen) |

Giant-page notes:
- Story p11 (hen): the hen sits high on the perch, out of a small child's
  reach, so the picture shows {name} at the foot of the perch pointing up.
- Story p12 (harp): the plate's harp is taller than the child; new staging
  `harp_by_the_chair` puts {name} beside it, gripping the strap.
- The edit model widens the plate's chair to fit the giant. Accepted.
- Story p13 render: the harp on the child's back did not show.
- Story p14/p15 renders: the giant reads only about twice the child's size
  (he is set back in depth). Owner to judge.
- Story p16: a mid-swing axe kept hanging edge-down, so the page now shows the
  wind-up between chops. The first wind-up render drew two axes; "exactly one
  axe" fixed it.

None of this reaches new books until the stories are re-seeded and the plates
re-imported/approved (new template version: slots live with the plates).
