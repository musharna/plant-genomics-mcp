# Response-size figure for the ARF dossier's 16 chain tools.
#
# The brief's original figure — calls per tool, all 50 tools — carries no
# information: every chain tool was called exactly 3 times (one per gene)
# and the other 34 were never called, so it would draw as 16 equal-height
# bars and 34 blanks. `coverage.tsv` still has a row for all 50 (built by
# `coverage.py`); this figure instead plots the one thing that DOES vary
# per call among the 16 chain tools — response size — at per-call
# granularity, one point per gene call (3 per tool), from `calls.jsonl`
# directly rather than from the aggregated table. `n_bytes` there is the
# length of the JSON-RPC response LINE, which carries each payload as both
# `content[].text` and `structuredContent` — a transport figure, not the
# size of the tool's own payload, so every size label on this figure says
# "on the wire".
#
# Colour (fill) is a per-TOOL property, `release_status` from
# `coverage.tsv` — computed in coverage.py from calls.jsonl plus
# gaps.jsonl's "version-under-another-key" row, never re-derived here —
# so all three of a tool's points share one fill. Round 1 of the visual
# review found two defects in the first version: (1) the two-level colour
# ("reports upstream release" / "no release reported") asserted an absence
# that gaps.jsonl's own hand-logged row contradicts for three tools, and
# (2) random jitter plus solid points let identical byte counts (e.g.
# kegg_pathways, 724 B on all three genes) draw as a single dot, hiding
# 25 of 48 points. Both are fixed below: release_status is the real
# three-level table column, and each point gets a deterministic offset
# plus a white outline so ties stay countable.
#
# Task 7 (full family, three organisms): one row of calls.jsonl is one MCP
# call, and eight chain tools now go through their batch_ form (one call
# per organism per 50 loci) while the other eight are still called once
# per locus. So a point is one CALL, not one gene; the per-locus tools
# draw a hundred-odd points per row and the batch tools draw three to six.
# The per-gene offset (3 levels) became a per-ORGANISM offset (3 levels):
# one point per gene per tool is no longer readable at this count, and the
# organism is what the figure has to let the eye separate. The caption
# says so. Calls of kind "expected" (a documented organism refusal, e.g.
# kegg_pathways on wheat) are drawn hollow: they are answers, not data.

library(ggplot2)
library(jsonlite)

source("examples/arf_family/theme_pgmcp.R")

cov <- read.delim("examples/arf_family/coverage.tsv")
chain <- cov[cov$status != "unused", ]

calls <- jsonlite::stream_in(file("examples/arf_family/calls.jsonl"), verbose = FALSE)
calls <- calls[calls$tool %in% chain$tool, ]

tool_order <- chain$tool[order(chain$median_bytes)]
calls$tool <- factor(calls$tool, levels = tool_order)

release_lookup <- setNames(chain$release_status, chain$tool)
release_levels <- c("upstream_version field", "release under another key", "no release in the payload")
calls$release_status <- factor(release_lookup[as.character(calls$tool)], levels = release_levels)

# Deterministic per-organism vertical offset, by sorted organism slug —
# replaces random jitter, which drew a different figure on every render
# and still overlapped exact ties.
organisms_sorted <- sort(unique(calls$organism))
stopifnot(length(organisms_sorted) <= 3)
organism_offset <- setNames(c(-0.25, 0, 0.25)[seq_along(organisms_sorted)], organisms_sorted)
calls$y <- as.numeric(calls$tool) + organism_offset[calls$organism]
calls$organism <- factor(calls$organism, levels = organisms_sorted)
if (is.null(calls$kind)) calls$kind <- ifelse(calls$ok, "ok", "error")
calls$expected <- calls$kind == "expected"

n_calls <- nrow(calls)
n_genes <- nrow(read.delim("examples/arf_family/genes.tsv"))

oversize_threshold <- 200000  # bytes; the dossier runner's gaps_auto.jsonl cutoff

min_bytes <- min(calls$n_bytes)
max_bytes <- max(calls$n_bytes)
fold <- round(max_bytes / min_bytes)
n_upstream_field <- sum(chain$release_status == "upstream_version field")
n_chain <- nrow(chain)

title_line1 <- sprintf(
  "Response sizes on the wire span %s×, from %s B to %s kB",
  fold, format(min_bytes, big.mark = ","), round(max_bytes / 1000)
)
title_line2 <- sprintf(
  "only %d of %d chain tools name the release under upstream_version",
  n_upstream_field, n_chain
)

p <- ggplot(calls, aes(x = n_bytes, y = y)) +
  geom_vline(
    aes(xintercept = oversize_threshold, linetype = "200 kB oversize threshold"),
    colour = pgmcp_refline_colour
  ) +
  geom_point(
    data = calls[!calls$expected, ],
    aes(fill = release_status, shape = organism),
    colour = pgmcp_point_outline,
    stroke = pgmcp_point_stroke,
    size = pgmcp_point_size,
    alpha = 0.85
  ) +
  geom_point(
    data = calls[calls$expected, ],
    aes(shape = organism),
    fill = NA,
    colour = pgmcp_refline_colour,
    stroke = pgmcp_point_stroke,
    size = pgmcp_point_size
  ) +
  scale_shape_manual(
    name = NULL,
    values = c(21, 22, 24)[seq_along(organisms_sorted)],
    breaks = organisms_sorted,
    labels = gsub("_", " ", organisms_sorted)
  ) +
  scale_x_log10(labels = scales::label_comma()) +
  scale_y_continuous(
    breaks = seq_along(levels(calls$tool)),
    labels = levels(calls$tool),
    limits = c(0.5, length(levels(calls$tool)) + 0.5),
    expand = c(0, 0)
  ) +
  scale_fill_manual(
    values = pal_pgmcp, breaks = release_levels,
    # Two rows: the three release_status labels together don't fit one row
    # at the figure's fixed 7in width without clipping (round 1 of the
    # visual review caught "no release in the payload" truncating off the
    # right edge).
    guide = ggplot2::guide_legend(nrow = 2)
  ) +
  scale_linetype_manual(name = NULL, values = c("200 kB oversize threshold" = "dashed")) +
  labs(
    title = paste0(title_line1, "\n", title_line2),
    x = "response size on the wire, bytes (log10 scale)",
    y = NULL,
    fill = NULL,
    caption = sprintf(
      paste0(
        "One point per MCP call (%d calls over %d genes in %d organisms; batch_* calls cover up to 50 loci each),\n",
        "offset vertically by organism, shape by organism; hollow = documented organism refusal (expected). ",
        "Tools ordered by median size."
      ),
      n_calls, n_genes, length(organisms_sorted)
    )
  ) +
  theme_pgmcp()

ggsave("examples/arf_family/coverage.png", p, width = 7, height = 6, dpi = 200)
