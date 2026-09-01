"""Symbol picker for footprints the matcher could not identify confidently.

The point of this dialog is to make the decision answerable at a glance: the ranked
candidates, *why* each was suggested, and the pad-versus-pin comparison that is the
usual reason a plausible-looking symbol is actually wrong.
"""

import wx

from ..core import matcher


class SymbolPickerDialog(wx.Dialog):
    def __init__(self, parent, info, candidates, index, remaining=0):
        # Lead with the reference designator. On a 90-part board "which DIP-14 is
        # this?" is the first thing the user needs answered, and the footprint name
        # alone cannot tell them -- half the board shares it.
        who = info.reference or "(unannotated)"
        title = "Choose a symbol for %s" % who
        wx.Dialog.__init__(self, parent, title=title,
                           size=(880, 600),
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.info = info
        self.index = index
        self.all_candidates = candidates
        self.filtered = list(candidates)
        self.selection = None
        self.skip_all = False

        outer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(self, label="%s    %s" % (who, info.value or ""))
        font = heading.GetFont()
        font.SetPointSize(font.GetPointSize() + 3)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        heading.SetFont(font)
        outer.Add(heading, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

        x, y = info.pos
        header = wx.StaticText(self, label=(
            "Footprint:  %s\nValue:      %s\nPads (%d):  %s\nOn board:   (%.2f, %.2f) mm"
            % (info.lib_id,
               info.value or "(none)",
               len(info.pads), ", ".join(info.pads) or "none",
               x / 1e6, y / 1e6)
        ))
        outer.Add(header, 0, wx.ALL | wx.EXPAND, 10)

        if remaining:
            outer.Add(wx.StaticText(self, label="%d more footprint(s) after this one."
                                    % remaining), 0, wx.LEFT | wx.BOTTOM, 10)

        search_row = wx.BoxSizer(wx.HORIZONTAL)
        search_row.Add(wx.StaticText(self, label="Search:"), 0,
                       wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.search = wx.TextCtrl(self)
        self.search.Bind(wx.EVT_TEXT, self._on_search)
        search_row.Add(self.search, 1)
        outer.Add(search_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.InsertColumn(0, "Symbol", width=250)
        self.list.InsertColumn(1, "Pins", width=60)
        self.list.InsertColumn(2, "Ref", width=50)
        self.list.InsertColumn(3, "Why this was suggested", width=430)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activate)
        outer.Add(self.list, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        self.detail = wx.StaticText(self, label=" ")
        outer.Add(self.detail, 0, wx.ALL | wx.EXPAND, 10)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_select)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.skip_btn = wx.Button(self, wx.ID_ANY, "Skip this footprint")
        self.skip_all_btn = wx.Button(self, wx.ID_ANY, "Skip all remaining")
        self.ok_btn = wx.Button(self, wx.ID_OK, "Use selected symbol")
        self.ok_btn.SetDefault()
        self.skip_btn.Bind(wx.EVT_BUTTON, self._on_skip)
        self.skip_all_btn.Bind(wx.EVT_BUTTON, self._on_skip_all)
        buttons.Add(self.skip_btn, 0, wx.RIGHT, 6)
        buttons.Add(self.skip_all_btn, 0, wx.RIGHT, 6)
        buttons.AddStretchSpacer()
        buttons.Add(wx.Button(self, wx.ID_CANCEL, "Cancel conversion"), 0, wx.RIGHT, 6)
        buttons.Add(self.ok_btn, 0)
        outer.Add(buttons, 0, wx.ALL | wx.EXPAND, 10)

        self.SetSizer(outer)
        self._populate()

    # -- data ---------------------------------------------------------------

    def _populate(self):
        self.list.DeleteAllItems()
        for row, cand in enumerate(self.filtered):
            self.list.InsertItem(row, cand.lib_id)
            self.list.SetItem(row, 1, str(len(cand.entry.pins)))
            self.list.SetItem(row, 2, cand.entry.reference)
            self.list.SetItem(row, 3, "; ".join(cand.reasons))
        if self.filtered:
            self.list.Select(0)
            self.list.Focus(0)

    def _on_search(self, _evt):
        term = self.search.GetValue().strip().lower()
        if not term:
            self.filtered = list(self.all_candidates)
        else:
            # Searching goes beyond the ranked list: the right symbol may not have
            # scored at all, and refusing to show it would leave no way forward.
            seen = set()
            out = []
            for cand in self.all_candidates:
                if term in cand.lib_id.lower():
                    out.append(cand)
                    seen.add(cand.lib_id)
            for entry in self.index.entries:
                if len(out) >= 200:
                    break
                if entry.lib_id in seen or entry.is_power:
                    continue
                if term in entry.lib_id.lower():
                    out.append(matcher.Candidate(
                        entry, 0.0, ["found by search"],
                        {p: p for p in self.info.pads},
                    ))
            self.filtered = out
        self._populate()

    def _on_select(self, _evt):
        cand = self._current()
        if cand is None:
            return
        pads = set(self.info.pads)
        pins = set(cand.entry.pins)
        notes = []
        if pins and pads != pins:
            missing = sorted(pads - pins)
            extra = sorted(pins - pads)
            if missing:
                notes.append("pads with no matching pin: %s" % ", ".join(missing))
            if extra:
                notes.append("symbol pins unused: %s" % ", ".join(extra[:8]))
        if cand.is_positional:
            notes.append("pads will be mapped to pins in order -- check polarity")
        if cand.entry.description:
            notes.insert(0, cand.entry.description)
        self.detail.SetLabel("  |  ".join(notes) if notes else "Pins match the pads exactly.")

    def _current(self):
        idx = self.list.GetFirstSelected()
        if idx < 0 or idx >= len(self.filtered):
            return None
        return self.filtered[idx]

    # -- events -------------------------------------------------------------

    def _on_activate(self, _evt):
        if self._current():
            self.EndModal(wx.ID_OK)

    def _on_skip(self, _evt):
        self.selection = None
        self.EndModal(wx.ID_NO)

    def _on_skip_all(self, _evt):
        self.selection = None
        self.skip_all = True
        self.EndModal(wx.ID_NO)

    def result(self):
        return self._current()
