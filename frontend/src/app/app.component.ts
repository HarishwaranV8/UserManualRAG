
import { Component, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { marked } from 'marked';

interface Source {
  phone: string;
  source: string;
  page: number | null;
}

interface Message {
  role: 'user' | 'assistant';
  text: string;
  html?: string;
  sources?: Source[];
}

interface ChatResponse {
  answer?: string;
  in_scope?: boolean;
  phones?: string[];
  sources?: Source[];
  error?: string;
}

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './app.component.html',
  styleUrl: './app.component.css',
})
export class AppComponent {
  constructor(private cdr: ChangeDetectorRef) {}

  question = '';
  loading = false;
  error = '';

  messages: Message[] = [
    {
      role: 'assistant',
      text: 'Ask a question about a smartphone feature. I will compare all three manuals.',
    },
  ];

  async send(): Promise<void> {
    const question = this.question.trim();

    if (!question || this.loading) {
      return;
    }

    this.messages.push({
      role: 'user',
      text: question,
    });

    this.question = '';
    this.loading = true;
    this.error = '';
    this.cdr.detectChanges();

    try {
      const response = await fetch(
        'https://usermanualrag.onrender.com/api/chat/',
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ question }),
        }
      );

      const data: ChatResponse = await response.json();

      if (!response.ok) {
        throw new Error(
          data.error || `Request failed (${response.status})`
        );
      }

      const answer = String(
        data.answer || 'No answer returned.'
      );

      const html = await marked.parse(answer);

      // The LLM determines whether the question is in scope.
      // For unrelated questions, hide all retrieved sources.
      const sources: Source[] =
        data.in_scope === true
          ? (data.sources ?? [])
          : [];

      this.messages.push({
        role: 'assistant',
        text: answer,
        html,
        sources,
      });

      this.cdr.detectChanges();

      // Scroll to the latest message.
      setTimeout(() => {
        const container = document.querySelector('.messages');

        if (container) {
          container.scrollTop = container.scrollHeight;
        }
      });
    } catch (err: unknown) {
      this.error =
        err instanceof Error
          ? err.message
          : 'Could not connect to Django.';

      this.cdr.detectChanges();
    } finally {
      this.loading = false;
      this.cdr.detectChanges();
    }
  }

  onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void this.send();
    }
  }

  trackByIndex(index: number): number {
    return index;
  }
}
