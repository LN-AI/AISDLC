// What a basket line costs. The fix: a line costs its price times how many were asked for.

function lineTotal(line) {
  return line.price * line.qty;
}

function basketTotal(lines) {
  return lines.reduce(function (sum, line) { return sum + lineTotal(line); }, 0);
}

function money(amount) {
  return 'GBP ' + amount.toFixed(2);
}
