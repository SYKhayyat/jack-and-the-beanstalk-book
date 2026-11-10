# Book fixes (from the downloaded PDF review)

PDF page = story page + 1 (cover is page 1). Plate N = story page N.

Status rule: FIXED only when a freshly rendered page shows it. A seed or
staging edit with no render behind it is NOT TESTED.

| # | PDF page | Defect | Decision | Status |
|---|---|---|---|---|
| 1 | cover | lines on cover | gradient image, not stacked strips | NOT TESTED (4221010) |
| 2 | 3 | 4 beans | beans in one row, gaps | NOT TESTED |
| 3 | 4 | mother a floating torso, too big | `window_and_door` staging, waist-up in window | NOT TESTED |
| 4 | 5 | generic main character | plate-edit cast line called every girl "he/his"; now uses the star's own pronouns | pronoun bug fixed; likeness NOT TESTED |
| 5 | 7 | walks away from castle | `toward_the_castle` staging, side view facing left | NOT TESTED |
| 6 | 8,9,12,13,14 | giant standing / under table / looks at child | the chair faces the camera, so he sits in it facing the viewer, chin on chest; the child is ~0.21 of the frame (head below the seat); standing giant on story p13 is cut off by the top edge (`slot_line_cut`) | FIXED in test renders of story pages 7, 8, 11, 12, 13 (2026-11-10) |
| 7 | 10,11 | different window frame/view | plate 9 patched with ImageMagick | NOT TESTED |
| 8 | 14 | much darker for no reason | plate 13 brightened with ImageMagick | story p13 render (2026-11-10) is warmer and only a little darker than p12; owner to judge the look |
| 9 | 15 | runs away from beanstalk | pinned to the right, side view | NOT TESTED |
| 10 | 16 | giant not on stalk | hands on stem, boots on leaves | NOT TESTED |
| 11 | 17 | axe points down | `at_the_stalk` staging, level swing | NOT TESTED |
| 12 | 18 | giant falls wrong way | giant right of stalk, falls the same way | NOT TESTED |
| 12b | 19 | text says "on the step", hen missing | hen dropped from text | NOT TESTED |

Giant-page notes:
- Story p11 (hen): the hen sits high on the perch, out of a small child's
  reach, so the picture shows {name} at the foot of the perch pointing up.
- Story p12 (harp): the plate's harp is taller than the child; new staging
  `harp_by_the_chair` puts {name} beside it, gripping the strap.
- The edit model widens the plate's chair to fit the giant. Accepted.
- Story p13 render: the harp on the child's back did not show.

None of this reaches new books until the stories are re-seeded and the plates
re-imported/approved (new template version: slots live with the plates).
