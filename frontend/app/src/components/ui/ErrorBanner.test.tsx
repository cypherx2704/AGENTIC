import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ErrorBanner } from './ErrorBanner';
import { BffError } from '@/lib/bff-client';

describe('ErrorBanner', () => {
  it('renders the Contract-2 code, message and trace id from a BffError', () => {
    const err = new BffError(403, {
      code: 'FORBIDDEN',
      message: 'You lack the required scope.',
      trace_id: 'trace-123',
      request_id: 'req-456',
    });
    render(<ErrorBanner error={err} />);
    expect(screen.getByText('FORBIDDEN')).toBeInTheDocument();
    expect(screen.getByText('You lack the required scope.')).toBeInTheDocument();
    expect(screen.getByText(/trace-123/)).toBeInTheDocument();
  });

  it('renders a plain Error message', () => {
    render(<ErrorBanner error={new Error('boom')} />);
    expect(screen.getByText('boom')).toBeInTheDocument();
  });

  it('renders nothing when there is no error', () => {
    const { container } = render(<ErrorBanner error={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('uses the alert role for accessibility', () => {
    render(<ErrorBanner error={new Error('x')} />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });
});
