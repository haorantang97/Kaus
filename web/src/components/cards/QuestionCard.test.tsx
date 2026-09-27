import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QuestionCard } from './QuestionCard';
import type { InteractionItem } from './timelineReducer';

const item: InteractionItem = {
  kind:'interaction', interactionKind:'question', itemId:'question:1', order:1, lastSequence:1,
  terminal:false, runId:'r1', parentRunId:null, permission:null, authentication:null,
  status:'pending', decision:null, outcome:null,
  question:{requestId:'q1', prompt:'Choose', allowFreeText:false, allowMultiple:true,
    options:[{optionId:'a',label:'Alpha'}, {optionId:'b',label:'Beta'}]},
};

describe('question interactions', () => {
  it('holds multiple selections until submitted and supports deselection', async () => {
    const respond = vi.fn();
    const user = userEvent.setup();
    render(<QuestionCard item={item} onRespond={respond}/>);
    const alpha = screen.getByRole('button',{name:'Alpha'});
    await user.click(alpha);
    await user.click(screen.getByRole('button',{name:'Beta'}));
    expect(respond).not.toHaveBeenCalled();
    expect(alpha).toHaveAttribute('aria-pressed','true');
    await user.click(alpha);
    await user.click(screen.getByRole('button',{name:'提交'}));
    expect(respond).toHaveBeenCalledWith({optionIds:['b']});
  });
  it('cancels without submitting any selected option', async () => {
    const respond=vi.fn();
    render(<QuestionCard item={item} onRespond={respond}/>);
    await userEvent.click(screen.getByRole('button',{name:'取消'}));
    expect(respond).toHaveBeenCalledExactlyOnceWith({cancelled:true});
  });
  it('keeps read-only historical cards non-interactive', () => {
    render(<QuestionCard item={item}/>);
    expect(screen.getByRole('button',{name:'Alpha'})).toBeDisabled();
    expect(screen.queryByRole('button',{name:'取消'})).toBeNull();
  });
});
