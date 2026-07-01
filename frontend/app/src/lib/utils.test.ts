import { describe, expect, it } from 'vitest';
import { cn, formatCost, formatDuration, formatNumber, shortId } from './utils';

describe('cn', () => {
  it('joins truthy class names and drops falsy ones', () => {
    expect(cn('a', false, null, undefined, '', 'b')).toBe('a b');
  });
});

describe('formatCost', () => {
  it('renders zero as $0.00', () => {
    expect(formatCost(0)).toBe('$0.00');
  });
  it('renders sub-cent amounts with high precision', () => {
    expect(formatCost(0.000174)).toBe('$0.000174');
  });
  it('renders dashes for null/undefined', () => {
    expect(formatCost(null)).toBe('—');
    expect(formatCost(undefined)).toBe('—');
  });
});

describe('formatNumber', () => {
  it('adds thousands separators', () => {
    expect(formatNumber(12345)).toBe('12,345');
  });
  it('handles null', () => {
    expect(formatNumber(null)).toBe('—');
  });
});

describe('formatDuration', () => {
  it('renders ms under one second', () => {
    expect(formatDuration(640)).toBe('640 ms');
  });
  it('renders seconds at/over one second', () => {
    expect(formatDuration(2100)).toBe('2.10 s');
  });
  it('handles null', () => {
    expect(formatDuration(null)).toBe('—');
  });
});

describe('shortId', () => {
  it('truncates long ids with an ellipsis', () => {
    expect(shortId('0123456789abcdef', 8)).toBe('01234567…');
  });
  it('leaves short ids intact', () => {
    expect(shortId('abc')).toBe('abc');
  });
  it('renders a dash for empty', () => {
    expect(shortId(null)).toBe('—');
  });
});
