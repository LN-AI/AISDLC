// What a basket line costs. This is the whole of the bug, and it is deliberately one
// function: `fixed/` replaces this file and nothing else, so a suite that tells the two
// variants apart is telling them apart on the arithmetic rather than on incidental noise.

// BUG: the quantity is ignored, so a line costs the same whether you asked for one of the
// item or six of it. This is invisible at a quantity of one -- which is the point. It is the
// shape of defect that survives a demo, because a demo adds one of a thing and looks at the
// total, and only a customer who buys two ever sees it.
function lineTotal(line) {
  return line.price;
}

function basketTotal(lines) {
  return lines.reduce(function (sum, line) { return sum + lineTotal(line); }, 0);
}

function money(amount) {
  return 'GBP ' + amount.toFixed(2);
}
