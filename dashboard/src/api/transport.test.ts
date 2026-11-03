import { describe, expect, it } from 'vitest';
import { HttpTransport } from './transport';

// The edge image URL is derived on the client, so it must match the backend route
// GET /api/calibration/references/{ref_id}/{key_slug}.edges.png exactly.
describe('HttpTransport.edgesUrl', () => {
  const t = new HttpTransport();
  it('maps a reference still to its .edges.png companion', () => {
    expect(t.edgesUrl('/api/calibration/references/ref_1/fake_0_0de5fe0a.jpg')).toBe(
      '/api/calibration/references/ref_1/fake_0_0de5fe0a.edges.png',
    );
  });
  it('drops a query string on the still URL', () => {
    expect(t.edgesUrl('/api/calibration/references/ref_1/cam.jpg?v=2')).toBe(
      '/api/calibration/references/ref_1/cam.edges.png',
    );
  });
});
