# Answer-size figure for the ARF dossier's 16 chain tools.
#
# A calls-per-tool figure over every published tool carries no
# information: the tools outside the chain are never called and the 16
# inside it are called by a fixed rule (one batch call per organism per 50
# loci). `coverage.tsv` still has a row for every tool (built by
# `coverage.py`); this figure instead plots the one thing that DOES vary
# per call — how large an answer is — from `calls.jsonl` directly rather
# than from the aggregated table. The x value is `max_answer_chars`: the
# largest single locus's answer in the call, measured as a client reads
# it (one JSON copy). That is the quantity the runner's oversize check
# tests, against the dashed line. `n_bytes`, the JSON-RPC response line on
# the wire, is not plotted: it carries every payload twice and sums up to
# 50 loci, so it measures the batch, not an answer.
#
# Colour (fill) is a per-TOOL property, `release_status` from
# `coverage.tsv` — computed in coverage.py from calls.jsonl plus
# gaps.jsonl's "version-under-another-key" row, never re-derived here —
# so all of a tool's points share one fill. Two properties this figure
# must keep: (1) the colour is the three-level table column, because a
# two-level "reports a release / does not" would assert an absence that
# gaps.jsonl's own row contradicts for the tools that report one under
# another key; (2) every position is deterministic — no jitter — so the
# same log draws the same figure and the page's counts can be checked
# against it.
#
# One row of calls.jsonl is one MCP call. Every chain tool goes through a
# batch form — eight through their own batch_ tool, eight through
# batch_locus_call — one call per organism per 50 loci, so each tool draws
# one point per call that answered at least one locus. The vertical
# offset is per ORGANISM, not per gene: one point per gene per tool is not
# readable at this count, and the organism is what the eye has to
# separate. The shape legend and the caption both name the organism
# encoding. A call that answered no locus (every locus refused, as the
# tool documents for that organism, or failed) has no answer size; it is
# not drawn, and the caption counts it.

library(ggplot2)
library(jsonlite)

source("examples/arf_family/theme_pgmcp.R")

cov <- read.delim("examples/arf_family/coverage.tsv")
chain <- cov[cov$status != "unused", ]

calls <- jsonlite::stream_in(file("examples/arf_family/calls.jsonl"), verbose = FALSE)
stopifnot(!is.null(calls$max_answer_chars))
# A batch_locus_call call is counted under the tool it ran, the same key
# coverage.py's `call_key` builds (#131): pooled under one name, eight
# tools would share one row.
generic <- calls$tool == "batch_locus_call"
calls$tool[generic] <- paste0("batch_locus_call:", calls$chain_tool[generic])
calls <- calls[calls$tool %in% chain$tool, ]

n_calls <- nrow(calls)
n_genes <- nrow(read.delim("examples/arf_family/genes.tsv"))
answered <- calls$max_answer_chars > 0
n_unanswered <- sum(!answered)
calls <- calls[answered, ]

# Tools ordered by the median of what is plotted, over the drawn calls.
tool_median <- tapply(calls$max_answer_chars, calls$tool, median)
tool_order <- names(sort(tool_median))
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
# Exact ties inside one organism (the same tool's largest answer the same
# size on several calls) draw on ONE point. A glyph is about a third of a
# row's height, so a tie cannot be separated inside its row, and pushing
# it out of the row would misattribute the tool. The caption says so.
calls$organism <- factor(calls$organism, levels = organisms_sorted)

# The runner's OVERSIZE_CHARS: Claude Code's 25,000-token default cap at the
# fewest characters per token counted (1.88).
oversize_chars <- 47000
# A bound, not an equivalence: 47,000 characters is 18,100-25,000 tokens
# over the ratios counted. It applies to ONE answer; a batch reply as a
# whole can be far over it (the page says so).
cap_label <- "47,000 chars per answer (< 25k tokens)"

min_chars <- min(calls$max_answer_chars)
max_chars <- max(calls$max_answer_chars)
fold <- round(max_chars / min_chars)
n_upstream_field <- sum(chain$release_status == "upstream_version field")
n_chain <- nrow(chain)

title_line1 <- sprintf(
  "Largest answer per call: %s to %s characters (%s×)",
  format(min_chars, big.mark = ","), format(max_chars, big.mark = ","),
  format(fold, big.mark = ",")
)
title_line2 <- sprintf(
  "%d of %d tools called name the release under upstream_version",
  n_upstream_field, n_chain
)

p <- ggplot(calls, aes(x = max_answer_chars, y = y)) +
  geom_vline(
    aes(xintercept = oversize_chars, linetype = cap_label),
    colour = pgmcp_refline_colour
  ) +
  geom_point(
    aes(fill = release_status, shape = organism),
    colour = pgmcp_point_outline,
    stroke = pgmcp_point_stroke,
    size = pgmcp_point_size,
    alpha = 0.85
  ) +
  scale_shape_manual(
    name = NULL,
    values = c(21, 22, 24)[seq_along(organisms_sorted)],
    breaks = organisms_sorted,
    # Abbreviated binomials: three full slugs overrun the 7in width.
    labels = sub("^(.)[a-z]+_", "\\U\\1. ", organisms_sorted, perl = TRUE),
    guide = ggplot2::guide_legend(override.aes = list(fill = "grey60"))
  ) +
  # Room right of the cap, so the line is not the plot's edge.
  scale_x_log10(
    labels = scales::label_comma(), breaks = c(100, 300, 1000, 3000, 10000, 30000),
    limits = c(NA, oversize_chars * 1.6)
  ) +
  scale_y_continuous(
    breaks = seq_along(levels(calls$tool)),
    labels = levels(calls$tool),
    limits = c(0.5, length(levels(calls$tool)) + 0.5),
    expand = c(0, 0)
  ) +
  scale_fill_manual(
    values = pal_pgmcp, breaks = release_levels,
    # Two rows: the three release_status labels together don't fit one row
    # at the figure's fixed 7in width without clipping ("no release in the
    # payload" truncates off the right edge in one row). With `shape`
    # mapped to organism the fill keys draw no glyph unless a shape is
    # forced onto them.
    # One row unless all three levels are present (see above); two rows
    # for two keys left a gap that read as two separate legends.
    guide = ggplot2::guide_legend(
      nrow = if (length(unique(calls$release_status)) > 2) 2 else 1,
      override.aes = list(shape = 21)
    )
  ) +
  scale_linetype_manual(name = NULL, values = setNames("dashed", cap_label)) +
  labs(
    title = paste0(title_line1, "\n", title_line2),
    x = "largest answer in the call, characters (log10)",
    y = NULL,
    fill = NULL,
    caption = sprintf(
      paste0(
        "%d calls, %d genes, %d organisms; a batch_* call covers up to 50 loci. Tools ordered by median.\n",
        "One point per distinct (organism, size) per tool. Not drawn: %d calls that answered no locus\n",
        "(aragwas_associations on rice and wheat, atted_coexpression and kegg_pathways on wheat)."
      ),
      n_calls, n_genes, length(organisms_sorted), n_unanswered
    )
  ) +
  theme_pgmcp()

ggsave("examples/arf_family/coverage.png", p, width = 7, height = 6, dpi = 200)
