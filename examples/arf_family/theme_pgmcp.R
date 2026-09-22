# House ggplot2 theme + palette for plant-genomics-mcp figures.
#
# `grep -rIl "theme_" --include=*.R . ~/plant-genomics-mcp-space` (task-5,
# step 1) found no existing `.R` file anywhere in this repo or in the
# sibling `~/plant-genomics-mcp-space` checkout — this is the first R code
# in the tree, so it is created here rather than reused.
#
# All ggplot2 styling for this dossier's figures lives here, sourced by the
# plotting scripts; the scripts themselves add only geoms/scales specific
# to one figure.

theme_pgmcp <- function(base_size = 12) {
  ggplot2::theme_minimal(base_size = base_size) +
    ggplot2::theme(
      panel.grid.minor = ggplot2::element_blank(),
      legend.position = "bottom",
      # A figure with two legends (e.g. a fill scale plus a reference-line
      # linetype) packs them into one row by default, which pushed the
      # second legend's label off the right edge of the coverage figure in
      # round 1 of the visual review. Stacking legends vertically instead
      # gives each its own full-width row.
      legend.box = "vertical",
      plot.title.position = "plot"
    )
}

# `ok` / `error` / `unused` are the three `coverage.tsv` `status` values.
#
# "upstream_version field" / "release under another key" / "no release in
# the payload" are `coverage.tsv`'s `release_status` values (the coverage
# figure). Round 1 of the visual review caught that the first version of
# this figure collapsed the middle case into "no release reported": three
# chain tools DO report a release, just under a field other than
# `upstream_version` (`gaps.jsonl`, kind `version-under-another-key`).
# Okabe-Ito blue/green/orange — colour-blind-safe as a triple, not just
# pairwise, and distinct from the ok/error/unused greens/reds/greys above.
pal_pgmcp <- c(
  ok = "#2E7D5B",
  error = "#C0392B",
  unused = "#BDBDBD",
  "upstream_version field" = "#0072B2",
  "release under another key" = "#009E73",
  "no release in the payload" = "#E69F00"
)

# Point styling for the coverage figure's per-call scatter (shape 21: fill
# carries release_status, a thin outline keeps overlapping same-tool points
# countable — round 1 of the visual review found 8+ rows drawing as a
# single dot for 3 identical byte counts). House rule: these constants live
# here, not inline in the plotting script.
pgmcp_point_size <- 2.4
pgmcp_point_stroke <- 0.5
pgmcp_point_outline <- "white"

# Colour for the oversize-threshold reference line.
pgmcp_refline_colour <- "grey40"
