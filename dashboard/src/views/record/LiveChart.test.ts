import { describe, expect, it } from 'vitest';
import { chartOptions } from './LiveChart';

/**
 * L7a §3.5: the y-axis clipped the second decimal of g magnitudes and uPlot's
 * legend spent a row on "Time: --". The options are unit-testable without a
 * canvas, which jsdom does not provide.
 */
describe('LiveChart options (L7a §3.5)', () => {
  const roles = new Map([
    ['dev-chest', 'chest'],
    ['dev-waist', 'waist'],
  ]);

  it('formats the g axis with two decimals in a wide-enough gutter', () => {
    const opts = chartOptions(480, 200, ['dev-chest', 'dev-waist'], roles);
    const y = opts.axes?.[1];
    expect(y?.size).toBeGreaterThanOrEqual(48);
    const values = y?.values;
    expect(values).toBeTypeOf('function');
    if (typeof values !== 'function') throw new Error('the g axis must format its labels');
    expect(values(null as never, [0, 1.234, 10.5], 1, 40, 0.5)).toEqual([
      '0.00',
      '1.23',
      '10.50',
    ]);
  });

  it('turns off the built-in legend so the role swatches are the only legend', () => {
    const opts = chartOptions(480, 200, ['dev-chest'], roles);
    expect(opts.legend?.show).toBe(false);
    // Series labels stay for accessibility/tooltips, but there is no "Time" series.
    expect(opts.series?.map((s) => s.label)).toEqual([undefined, 'chest']);
  });

  it('still sizes the plot to the measured box', () => {
    const opts = chartOptions(0, 0, ['dev-chest'], roles);
    expect(opts.width).toBe(100);
    expect(opts.height).toBe(80);
  });
});