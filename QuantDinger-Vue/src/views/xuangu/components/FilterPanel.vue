<template>
  <div class="filter-panel">
    <el-tabs v-model="activeTab" type="border-card">
      <!-- Tab 1: 基本面 -->
      <el-tab-pane label="基本面" name="fundamental">
        <el-row :gutter="12">
          <el-col :span="8">
            <div class="filter-group">
              <h4>估值指标</h4>
              <el-form size="mini">
                <el-form-item label="PE (市盈率)">
                  <div class="slider-wrapper">
                    <el-slider v-model="localFilters.pe_range" :min="sliderConfigs.pe.min" :max="sliderConfigs.pe.max" :step="sliderConfigs.pe.step" range @change="onSliderChange('pe_range', $event)" class="custom-slider"></el-slider>
                  </div>
                </el-form-item>
                <el-form-item label="PB (市净率)">
                  <div class="slider-wrapper">
                    <el-slider v-model="localFilters.pb_range" :min="sliderConfigs.pb.min" :max="sliderConfigs.pb.max" :step="sliderConfigs.pb.step" range @change="onSliderChange('pb_range', $event)" class="custom-slider"></el-slider>
                  </div>
                </el-form-item>
                <el-form-item label="股息率(%)">
                  <div class="slider-wrapper">
                    <el-slider v-model="localFilters.dividend_min" :min="0" :max="20" :step="0.1" :format-tooltip="v => v + '%'" @change="emitUpdate" class="custom-slider"></el-slider>
                  </div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="filter-group">
              <h4>成长能力</h4>
              <el-checkbox-group v-model="localFilters.growth_indicators" @change="emitUpdate">
                <el-checkbox label="netprofit_yoy_ratio">净利增长&gt;15%</el-checkbox>
                <el-checkbox label="toi_yoy_ratio">营收增长&gt;15%</el-checkbox>
                <el-checkbox label="basiceps_yoy_ratio">每股收益增长&gt;10%</el-checkbox>
                <el-checkbox label="income_growthrate_3y">营收3年复合增长 &gt; 10%</el-checkbox>
                <el-checkbox label="netprofit_growthrate_3y">净利润3年复合增长 &gt; 10%</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="filter-group">
              <h4>盈利能力</h4>
              <el-form size="mini">
                <el-form-item label="ROE(%)">
                  <div class="slider-wrapper">
                    <el-slider v-model="localFilters.roe_min" :min="-50" :max="100" :step="1" :format-tooltip="v => v + '%'" @change="emitUpdate" class="custom-slider"></el-slider>
                  </div>
                </el-form-item>
                <el-form-item label="毛利率(%)">
                  <div class="slider-wrapper">
                    <el-slider v-model="localFilters.sale_gpr_min" :min="-50" :max="100" :step="1" :format-tooltip="v => v + '%'" @change="emitUpdate" class="custom-slider"></el-slider>
                  </div>
                </el-form-item>
                <el-form-item label="销售净利率(%)">
                  <div class="slider-wrapper">
                    <el-slider v-model="localFilters.sale_npr_min" :min="-100" :max="100" :step="1" :format-tooltip="v => v + '%'" @change="emitUpdate" class="custom-slider"></el-slider>
                  </div>
                </el-form-item>
              </el-form>
              <el-checkbox-group v-model="localFilters.quality_indicators" @change="emitUpdate">
                <el-checkbox label="per_netcash_operate">经营现金流为正</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 2: 技术面 -->
      <el-tab-pane label="技术面" name="technical">
        <el-row :gutter="12">
          <el-col :span="8">
            <div class="filter-group">
              <h4>均线突破</h4>
              <el-checkbox-group v-model="localFilters.ma_breakthrough" @change="emitUpdate">
                <el-checkbox label="breakup_ma_5days">突破5日线</el-checkbox>
                <el-checkbox label="breakup_ma_10days">突破10日线</el-checkbox>
                <el-checkbox label="breakup_ma_20days">突破20日线</el-checkbox>
                <el-checkbox label="breakup_ma_60days">突破60日线</el-checkbox>
                <el-checkbox label="long_avg_array">长期均线多头排列</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="filter-group">
              <h4>技术指标</h4>
              <el-checkbox-group v-model="localFilters.tech_signals" @change="emitUpdate">
                <el-checkbox label="macd_golden_fork">MACD金叉</el-checkbox>
                <el-checkbox label="kdj_golden_fork">KDJ金叉</el-checkbox>
                <el-checkbox label="break_through">突破形态</el-checkbox>
                <el-checkbox label="upper_large_volume">放量上涨</el-checkbox>
                <el-checkbox label="down_narrow_volume">缩量下跌</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="filter-group">
              <h4>经典K线形态</h4>
              <el-checkbox-group v-model="localFilters.k_classic" @change="emitUpdate">
                <el-checkbox label="one_dayang_line">大阳线</el-checkbox>
                <el-checkbox label="two_dayang_lines">两阳夹一阴</el-checkbox>
                <el-checkbox label="rise_sun">阳包阴</el-checkbox>
                <el-checkbox label="morning_star">早晨之星</el-checkbox>
                <el-checkbox label="evening_star">黄昏之星</el-checkbox>
                <el-checkbox label="shooting_star">射击之星</el-checkbox>
                <el-checkbox label="bottom_cross_harami">底部十字孕线</el-checkbox>
                <el-checkbox label="top_cross_harami">顶部十字孕线</el-checkbox>
                <el-checkbox label="three_black_crows">三只乌鸦</el-checkbox>
                <el-checkbox label="hammer">锤头</el-checkbox>
                <el-checkbox label="inverted_hammer">倒锤头</el-checkbox>
                <el-checkbox label="doji">十字星</el-checkbox>
                <el-checkbox label="long_legged_doji">长腿十字线</el-checkbox>
                <el-checkbox label="gravestone">墓碑线</el-checkbox>
                <el-checkbox label="dragonfly">蜻蜓线</el-checkbox>
                <el-checkbox label="two_flying_crows">双飞乌鸦</el-checkbox>
                <el-checkbox label="lotus_emerge">出水芙蓉</el-checkbox>
                <el-checkbox label="low_open_high">低开高走</el-checkbox>
                <el-checkbox label="huge_volume">巨量</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="filter-group">
              <h4>分时K线形态</h4>
              <el-checkbox-group v-model="localFilters.k_intraday" @change="emitUpdate">
                <el-checkbox label="tail_plate_rise">尾盘拉升</el-checkbox>
                <el-checkbox label="intraday_pressure">盘中打压</el-checkbox>
                <el-checkbox label="intraday_rise">盘中拉升</el-checkbox>
                <el-checkbox label="quick_rebound">快速反弹</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="filter-group">
              <h4>其它形态</h4>
              <el-checkbox-group v-model="localFilters.k_other" @change="emitUpdate">
                <el-checkbox label="limit_up">一字涨停</el-checkbox>
                <el-checkbox label="limit_down">一字跌停</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 3: 资金面 -->
      <el-tab-pane label="资金面" name="capital">
        <el-row :gutter="12">
          <el-col :span="8">
            <div class="filter-group">
              <h4>资金流向</h4>
              <el-checkbox-group v-model="localFilters.capital_flow" @change="emitUpdate">
                <el-checkbox label="low_funds_inflow">主力资金净流入</el-checkbox>
                <el-checkbox label="high_funds_outflow">主力资金净流出 (可选)</el-checkbox>
                <el-checkbox label="netinflow_3days">近3日资金净流入</el-checkbox>
                <el-checkbox label="netinflow_5days">近5日资金净流入</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="filter-group">
              <h4>成交量</h4>
              <el-form size="mini">
                <el-form-item label="量比">
                  <div class="slider-wrapper">
                    <el-slider v-model="localFilters.volume_ratio_min" :min="0" :max="50" :step="0.1" @change="emitUpdate" class="custom-slider"></el-slider>
                  </div>
                </el-form-item>
                <el-form-item label="换手率(%)">
                  <div class="slider-wrapper">
                    <el-slider v-model="localFilters.turnoverrate_min" :min="0" :max="50" :step="0.1" :format-tooltip="v => v + '%'" @change="emitUpdate" class="custom-slider"></el-slider>
                  </div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="filter-group">
              <h4>机构持股</h4>
              <el-checkbox-group v-model="localFilters.institutional_holding" @change="emitUpdate">
                <el-checkbox label="org_survey_3m">近3月有机构调研</el-checkbox>
                <el-checkbox label="allcorp_fund_ratio">基金重仓</el-checkbox>
                <el-checkbox label="allcorp_qs_ratio">券商重仓</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 4: 概念与行业 -->
      <el-tab-pane label="概念/行业" name="concept">
        <el-row :gutter="12">
          <el-col :span="12">
            <div class="filter-group">
              <h4>行业分类</h4>
              <el-select v-model="localFilters.industry" multiple placeholder="请选择行业" @change="emitUpdate" size="mini" filterable>
                <el-option label="新能源" value="新能源"></el-option>
                <el-option label="人工智能" value="人工智能"></el-option>
                <el-option label="半导体" value="半导体"></el-option>
                <el-option label="医药生物" value="医药生物"></el-option>
                <el-option label="食品饮料" value="食品饮料"></el-option>
                <el-option label="金融" value="金融"></el-option>
                <el-option label="房地产" value="房地产"></el-option>
                <el-option label="交通运输" value="交通运输"></el-option>
                <el-option label="公用事业" value="公用事业"></el-option>
                <el-option label="钢铁" value="钢铁"></el-option>
                <el-option label="有色金属" value="有色金属"></el-option>
                <el-option label="化工" value="化工"></el-option>
                <el-option label="建筑材料" value="建筑材料"></el-option>
                <el-option label="电子" value="电子"></el-option>
                <el-option label="电气设备" value="电气设备"></el-option>
                <el-option label="机械设备" value="机械设备"></el-option>
                <el-option label="汽车" value="汽车"></el-option>
                <el-option label="纺织服装" value="纺织服装"></el-option>
                <el-option label="轻工制造" value="轻工制造"></el-option>
                <el-option label="商业贸易" value="商业贸易"></el-option>
                <el-option label="休闲服务" value="休闲服务"></el-option>
                <el-option label="传媒" value="传媒"></el-option>
                <el-option label="计算机" value="计算机"></el-option>
                <el-option label="通信" value="通信"></el-option>
                <el-option label="农林牧渔" value="农林牧渔"></el-option>
                <el-option label="国防军工" value="国防军工"></el-option>
                <el-option label="建筑装饰" value="建筑装饰"></el-option>
              </el-select>
            </div>
          </el-col>
          <el-col :span="12">
            <div class="filter-group">
              <h4>概念题材</h4>
              <el-select v-model="localFilters.concept" multiple placeholder="请选择概念" @change="emitUpdate" size="mini" filterable>
                <el-option label="国企改革" value="国企改革"></el-option>
                <el-option label="一带一路" value="一带一路"></el-option>
                <el-option label="碳中和" value="碳中和"></el-option>
                <el-option label="新能源车" value="新能源车"></el-option>
                <el-option label="光伏" value="光伏"></el-option>
                <el-option label="储能" value="储能"></el-option>
                <el-option label="元宇宙" value="元宇宙"></el-option>
                <el-option label="芯片" value="芯片"></el-option>
                <el-option label="5G" value="5G"></el-option>
                <el-option label="云计算" value="云计算"></el-option>
                <el-option label="大数据" value="大数据"></el-option>
                <el-option label="区块链" value="区块链"></el-option>
              </el-select>
            </div>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 5: 行情指标 -->
      <el-tab-pane label="行情指标" name="market_indicator">
        <el-row :gutter="12">
          <el-col :span="6">
            <div class="filter-group">
              <h4>量价指标</h4>
              <el-form size="mini">
                <el-form-item label="量比">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_volume_ratio_min" :min="sliderConfigs.mi_volume_ratio.min" :max="sliderConfigs.mi_volume_ratio.max" :step="sliderConfigs.mi_volume_ratio.step" @change="emitUpdate" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="换手率(%)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_turnover_rate_min" :min="sliderConfigs.mi_turnover_rate.min" :max="sliderConfigs.mi_turnover_rate.max" :step="sliderConfigs.mi_turnover_rate.step" @change="emitUpdate" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="振幅(%)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_amplitude_range" :min="sliderConfigs.mi_amplitude.min" :max="sliderConfigs.mi_amplitude.max" :step="sliderConfigs.mi_amplitude.step" range @change="onSliderChange('mi_amplitude_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="成交量(手)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_volume_min" :min="sliderConfigs.mi_volume.min" :max="sliderConfigs.mi_volume.max" :step="sliderConfigs.mi_volume.step" @change="emitUpdate" class="custom-slider"></el-slider></div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="filter-group">
              <h4>估值 & 市值</h4>
              <el-form size="mini">
                <el-form-item label="成交额(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_amount_min" :min="sliderConfigs.mi_amount.min" :max="sliderConfigs.mi_amount.max" :step="sliderConfigs.mi_amount.step" @change="emitUpdate" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="市盈率">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_pe_range" :min="sliderConfigs.mi_pe.min" :max="sliderConfigs.mi_pe.max" :step="sliderConfigs.mi_pe.step" range @change="onSliderChange('mi_pe_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="流通市值(亿)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_float_mc_range" :min="sliderConfigs.mi_float_mc.min" :max="sliderConfigs.mi_float_mc.max" :step="sliderConfigs.mi_float_mc.step" range @change="onSliderChange('mi_float_mc_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="总市值(亿)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_total_mc_range" :min="sliderConfigs.mi_total_mc.min" :max="sliderConfigs.mi_total_mc.max" :step="sliderConfigs.mi_total_mc.step" range @change="onSliderChange('mi_total_mc_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="filter-group">
              <h4>涨跌</h4>
              <el-form size="mini">
                <el-form-item label="委比(%)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_comp_ratio_range" :min="sliderConfigs.mi_comp_ratio.min" :max="sliderConfigs.mi_comp_ratio.max" :step="sliderConfigs.mi_comp_ratio.step" range @change="onSliderChange('mi_comp_ratio_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="今日涨幅(%)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_today_up_range" :min="sliderConfigs.mi_today_up.min" :max="sliderConfigs.mi_today_up.max" :step="sliderConfigs.mi_today_up.step" range @change="onSliderChange('mi_today_up_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="5日涨幅(%)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_change_5d_range" :min="sliderConfigs.mi_change_5d.min" :max="sliderConfigs.mi_change_5d.max" :step="sliderConfigs.mi_change_5d.step" range @change="onSliderChange('mi_change_5d_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="10日涨幅(%)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_change_10d_range" :min="sliderConfigs.mi_change_10d.min" :max="sliderConfigs.mi_change_10d.max" :step="sliderConfigs.mi_change_10d.step" range @change="onSliderChange('mi_change_10d_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="filter-group">
              <h4>价格 & 资金</h4>
              <el-form size="mini">
                <el-form-item label="60日涨幅(%)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_change_60d_range" :min="sliderConfigs.mi_change_60d.min" :max="sliderConfigs.mi_change_60d.max" :step="sliderConfigs.mi_change_60d.step" range @change="onSliderChange('mi_change_60d_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="年初至今(%)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_change_ytd_range" :min="sliderConfigs.mi_change_ytd.min" :max="sliderConfigs.mi_change_ytd.max" :step="sliderConfigs.mi_change_ytd.step" range @change="onSliderChange('mi_change_ytd_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="收盘价(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_close_range" :min="sliderConfigs.mi_close.min" :max="sliderConfigs.mi_close.max" :step="sliderConfigs.mi_close.step" range @change="onSliderChange('mi_close_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="净流入(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.mi_net_in_range" :min="sliderConfigs.mi_net_in.min" :max="sliderConfigs.mi_net_in.max" :step="sliderConfigs.mi_net_in.step" range @change="onSliderChange('mi_net_in_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 6: 特殊筛选 -->
      <el-tab-pane label="特殊筛选" name="special_filter">
        <el-row :gutter="12">
          <el-col :span="8">
            <div class="filter-group">
              <h4>新高新低</h4>
              <el-checkbox-group v-model="localFilters.new_high_filter" @change="emitUpdate">
                <el-checkbox label="now_newhigh">当前新高</el-checkbox>
                <el-checkbox label="now_newlow">当前新低</el-checkbox>
                <el-checkbox label="high_recent_3days">最近3天新高</el-checkbox>
                <el-checkbox label="high_recent_5days">最近5天新高</el-checkbox>
                <el-checkbox label="high_recent_10days">最近10天新高</el-checkbox>
                <el-checkbox label="high_recent_20days">最近20天新高</el-checkbox>
                <el-checkbox label="high_recent_30days">最近30天新高</el-checkbox>
                <el-checkbox label="low_recent_3days">最近3天新低</el-checkbox>
                <el-checkbox label="low_recent_5days">最近5天新低</el-checkbox>
                <el-checkbox label="low_recent_10days">最近10天新低</el-checkbox>
                <el-checkbox label="low_recent_20days">最近20天新低</el-checkbox>
                <el-checkbox label="low_recent_30days">最近30天新低</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="filter-group">
              <h4>战胜大盘</h4>
              <el-checkbox-group v-model="localFilters.win_market_filter" @change="emitUpdate">
                <el-checkbox label="win_market_3days">最近3天战胜大盘</el-checkbox>
                <el-checkbox label="win_market_5days">最近5天战胜大盘</el-checkbox>
                <el-checkbox label="win_market_10days">最近10天战胜大盘</el-checkbox>
                <el-checkbox label="win_market_20days">最近20天战胜大盘</el-checkbox>
                <el-checkbox label="win_market_30days">最近30天战胜大盘</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="filter-group">
              <h4>连涨连跌 & K线形态</h4>
              <el-checkbox-group v-model="localFilters.consecutive_signals" @change="emitUpdate">
                <el-checkbox label="upper_4days">连续4天上涨</el-checkbox>
                <el-checkbox label="upper_8days">连续8天上涨</el-checkbox>
                <el-checkbox label="upper_9days">连续9天上涨</el-checkbox>
                <el-checkbox label="down_7days">连续7天下跌</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
        </el-row>
        <el-row :gutter="12" style="margin-top: 10px;">
          <el-col :span="8">
            <div class="filter-group">
              <h4>限价/定增/质押</h4>
              <el-checkbox-group v-model="localFilters.limited_lift_filter" @change="emitUpdate">
                <el-checkbox label="limited_lift_6m">限价上涨6月</el-checkbox>
                <el-checkbox label="limited_lift_1y">限价上涨1年</el-checkbox>
              </el-checkbox-group>
              <el-checkbox-group v-model="localFilters.directional_seo_filter" @change="emitUpdate">
                <el-checkbox label="directional_seo_1m">定向增发1月</el-checkbox>
                <el-checkbox label="directional_seo_3m">定向增发3月</el-checkbox>
                <el-checkbox label="directional_seo_6m">定向增发6月</el-checkbox>
                <el-checkbox label="directional_seo_1y">定向增发1年</el-checkbox>
              </el-checkbox-group>
              <el-checkbox-group v-model="localFilters.equity_pledge_filter" @change="emitUpdate">
                <el-checkbox label="equity_pledge_1m">股权质押1月</el-checkbox>
                <el-checkbox label="equity_pledge_3m">股权质押3月</el-checkbox>
                <el-checkbox label="equity_pledge_6m">股权质押6月</el-checkbox>
                <el-checkbox label="equity_pledge_1y">股权质押1年</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="filter-group">
              <h4>板块标识</h4>
              <el-checkbox-group v-model="localFilters.hs_board_filter" @change="emitUpdate">
                <el-checkbox label="is_sz50">上证50成分股</el-checkbox>
                <el-checkbox label="is_zz1000">中证1000成分股</el-checkbox>
                <el-checkbox label="is_cy50">创业板50成分股</el-checkbox>
                <el-checkbox label="is_bps_break">已破净</el-checkbox>
                <el-checkbox label="is_issue_break">已破板</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 7: 筹码指标 -->
      <el-tab-pane label="筹码指标" name="chip">
        <el-row :gutter="12">
          <el-col :span="6">
            <div class="filter-group">
              <h4>成本分布</h4>
              <el-form size="mini">
                <el-form-item label="成本价(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.ch_cost_price_range" :min="sliderConfigs.ch_cost_price.min" :max="sliderConfigs.ch_cost_price.max" :step="sliderConfigs.ch_cost_price.step" range @change="onSliderChange('ch_cost_price_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="获利比例(%)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.ch_profit_ratio_range" :min="sliderConfigs.ch_profit_ratio.min" :max="sliderConfigs.ch_profit_ratio.max" :step="sliderConfigs.ch_profit_ratio.step" range @change="onSliderChange('ch_profit_ratio_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="平均成本(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.ch_avg_cost_range" :min="sliderConfigs.ch_avg_cost.min" :max="sliderConfigs.ch_avg_cost.max" :step="sliderConfigs.ch_avg_cost.step" range @change="onSliderChange('ch_avg_cost_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="filter-group">
              <h4>集中度 & 股东</h4>
              <el-form size="mini">
                <el-form-item label="90%集中度(%)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.ch_conc_90_range" :min="sliderConfigs.ch_conc_90.min" :max="sliderConfigs.ch_conc_90.max" :step="sliderConfigs.ch_conc_90.step" range @change="onSliderChange('ch_conc_90_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="70%集中度(%)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.ch_conc_70_range" :min="sliderConfigs.ch_conc_70.min" :max="sliderConfigs.ch_conc_70.max" :step="sliderConfigs.ch_conc_70.step" range @change="onSliderChange('ch_conc_70_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="股东总数(户)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.ch_holder_count_range" :min="sliderConfigs.ch_holder_count.min" :max="sliderConfigs.ch_holder_count.max" :step="sliderConfigs.ch_holder_count.step" range @change="onSliderChange('ch_holder_count_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 8: 龙虎榜 -->
      <el-tab-pane label="龙虎榜" name="tiger">
        <el-row :gutter="12">
          <el-col :span="6">
            <div class="filter-group">
              <h4>上榜信息</h4>
              <el-form size="mini">
                <el-form-item label="上榜日期">
                  <div class="range-input">
                    <el-date-picker v-model="localFilters.tiger_date_min" type="date" placeholder="起始" size="small" value-format="yyyy-MM-dd" @change="emitUpdate" class="range-min"></el-date-picker>
                    <span class="range-label">~</span>
                    <el-date-picker v-model="localFilters.tiger_date_max" type="date" placeholder="截止" size="small" value-format="yyyy-MM-dd" @change="emitUpdate" class="range-max"></el-date-picker>
                  </div>
                </el-form-item>
                <el-form-item label="龙虎榜买入(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.tiger_buy_range" :min="sliderConfigs.tiger_buy.min" :max="sliderConfigs.tiger_buy.max" :step="sliderConfigs.tiger_buy.step" range @change="onSliderChange('tiger_buy_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="龙虎榜卖出(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.tiger_sell_range" :min="sliderConfigs.tiger_sell.min" :max="sliderConfigs.tiger_sell.max" :step="sliderConfigs.tiger_sell.step" range @change="onSliderChange('tiger_sell_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="filter-group">
              <h4>净买 & 参与方</h4>
              <el-form size="mini">
                <el-form-item label="龙虎榜净买入(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.tiger_net_range" :min="sliderConfigs.tiger_net.min" :max="sliderConfigs.tiger_net.max" :step="sliderConfigs.tiger_net.step" range @change="onSliderChange('tiger_net_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="营业部买入(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.tiger_dept_buy_range" :min="sliderConfigs.tiger_dept_buy.min" :max="sliderConfigs.tiger_dept_buy.max" :step="sliderConfigs.tiger_dept_buy.step" range @change="onSliderChange('tiger_dept_buy_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="机构买入(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.tiger_inst_buy_range" :min="sliderConfigs.tiger_inst_buy.min" :max="sliderConfigs.tiger_inst_buy.max" :step="sliderConfigs.tiger_inst_buy.step" range @change="onSliderChange('tiger_inst_buy_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
              </el-form>
              <el-checkbox-group v-model="localFilters.tiger_participant" @change="emitUpdate">
                <el-checkbox label="inst_participated">机构参与</el-checkbox>
                <el-checkbox label="dept_participated">营业部参与</el-checkbox>
              </el-checkbox-group>
            </div>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 9: 技术指标(均线) -->
      <el-tab-pane label="技术指标" name="tech_indicator">
        <el-row :gutter="12">
          <el-col :span="6">
            <div class="filter-group">
              <h4>均线价格</h4>
              <el-form size="mini">
                <el-form-item label="MA5(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.ti_ma5_range" :min="sliderConfigs.ti_ma5.min" :max="sliderConfigs.ti_ma5.max" :step="sliderConfigs.ti_ma5.step" range @change="onSliderChange('ti_ma5_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="MA10(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.ti_ma10_range" :min="sliderConfigs.ti_ma10.min" :max="sliderConfigs.ti_ma10.max" :step="sliderConfigs.ti_ma10.step" range @change="onSliderChange('ti_ma10_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="filter-group">
              <h4>&nbsp;</h4>
              <el-form size="mini">
                <el-form-item label="MA20(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.ti_ma20_range" :min="sliderConfigs.ti_ma20.min" :max="sliderConfigs.ti_ma20.max" :step="sliderConfigs.ti_ma20.step" range @change="onSliderChange('ti_ma20_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
                <el-form-item label="MA60(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.ti_ma60_range" :min="sliderConfigs.ti_ma60.min" :max="sliderConfigs.ti_ma60.max" :step="sliderConfigs.ti_ma60.step" range @change="onSliderChange('ti_ma60_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="filter-group">
              <h4>&nbsp;</h4>
              <el-form size="mini">
                <el-form-item label="MA120(元)">
                  <div class="slider-wrapper"><el-slider v-model="localFilters.ti_ma120_range" :min="sliderConfigs.ti_ma120.min" :max="sliderConfigs.ti_ma120.max" :step="sliderConfigs.ti_ma120.step" range @change="onSliderChange('ti_ma120_range', $event)" class="custom-slider"></el-slider></div>
                </el-form-item>
              </el-form>
            </div>
          </el-col>
        </el-row>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script>
// 滑块配置（内部常量）
const SLIDER_CONFIGS = {
  pe: { min: -100, max: 500, step: 1 },
  pb: { min: -50, max: 100, step: 0.1 },
  mi_volume_ratio: { min: 0, max: 100, step: 0.1 },
  mi_turnover_rate: { min: 0, max: 100, step: 0.1 },
  mi_amplitude: { min: 0, max: 50, step: 0.1 },
  mi_volume: { min: 0, max: 10000000, step: 10000 },
  mi_amount: { min: 0, max: 500000000000, step: 100000000 },
  mi_pe: { min: -200, max: 500, step: 1 },
  mi_float_mc: { min: 0, max: 20000, step: 10 },
  mi_total_mc: { min: 0, max: 50000, step: 10 },
  mi_comp_ratio: { min: -100, max: 100, step: 1 },
  mi_today_up: { min: -10, max: 10, step: 0.1 },
  mi_change_5d: { min: -50, max: 50, step: 0.1 },
  mi_change_10d: { min: -50, max: 50, step: 0.1 },
  mi_change_60d: { min: -100, max: 100, step: 0.1 },
  mi_change_ytd: { min: -200, max: 500, step: 0.1 },
  mi_close: { min: 0, max: 2000, step: 0.01 },
  mi_net_in: { min: -10000000000, max: 10000000000, step: 100000000 },
  ch_cost_price: { min: 0, max: 2000, step: 0.01 },
  ch_profit_ratio: { min: 0, max: 100, step: 0.1 },
  ch_avg_cost: { min: 0, max: 2000, step: 0.01 },
  ch_conc_90: { min: 0, max: 100, step: 0.1 },
  ch_conc_70: { min: 0, max: 100, step: 0.1 },
  ch_holder_count: { min: 0, max: 2000000, step: 10000 },
  tiger_buy: { min: 0, max: 100000000000, step: 100000000 },
  tiger_sell: { min: 0, max: 100000000000, step: 100000000 },
  tiger_net: { min: -50000000000, max: 50000000000, step: 100000000 },
  tiger_dept_buy: { min: 0, max: 50000000000, step: 100000000 },
  tiger_inst_buy: { min: 0, max: 50000000000, step: 100000000 },
  ti_ma5: { min: 0, max: 2000, step: 0.01 },
  ti_ma10: { min: 0, max: 2000, step: 0.01 },
  ti_ma20: { min: 0, max: 2000, step: 0.01 },
  ti_ma60: { min: 0, max: 2000, step: 0.01 },
  ti_ma120: { min: 0, max: 2000, step: 0.01 }
}

// 空筛选条件默认值
export function getDefaultFilters () {
  return {
    pe_min: null, pe_max: null, pb_min: null, pb_max: null, dividend_min: null,
    growth_indicators: [], quality_indicators: [], roe_min: null, sale_gpr_min: null, sale_npr_min: null,
    ma_breakthrough: [], tech_signals: [], k_classic: [], k_intraday: [], k_other: [],
    capital_flow: [], volume_ratio_min: null, turnoverrate_min: null, institutional_holding: [],
    industry: [], concept: [],
    mi_volume_ratio_min: null, mi_volume_ratio_max: null, mi_turnover_rate_min: null, mi_turnover_rate_max: null,
    mi_amplitude_min: null, mi_amplitude_max: null, mi_pe_min: null, mi_pe_max: null,
    mi_float_mc_min: null, mi_float_mc_max: null, mi_total_mc_min: null, mi_total_mc_max: null,
    mi_comp_ratio_min: null, mi_comp_ratio_max: null, mi_volume_min: null, mi_volume_max: null,
    mi_amount_min: null, mi_amount_max: null, mi_net_in_min: null, mi_net_in_max: null,
    mi_today_up_min: null, mi_today_up_max: null,
    mi_change_5d_min: null, mi_change_5d_max: null, mi_change_10d_min: null, mi_change_10d_max: null,
    mi_change_60d_min: null, mi_change_60d_max: null, mi_change_ytd_min: null, mi_change_ytd_max: null,
    mi_close_min: null, mi_close_max: null,
    ch_cost_price_min: null, ch_cost_price_max: null, ch_profit_ratio_min: null, ch_profit_ratio_max: null,
    ch_conc_90_min: null, ch_conc_90_max: null, ch_conc_70_min: null, ch_conc_70_max: null,
    ch_avg_cost_min: null, ch_avg_cost_max: null, ch_holder_count_min: null, ch_holder_count_max: null,
    tiger_date_min: null, tiger_date_max: null,
    tiger_buy_min: null, tiger_buy_max: null, tiger_sell_min: null, tiger_sell_max: null,
    tiger_net_min: null, tiger_net_max: null, tiger_dept_buy_min: null, tiger_dept_buy_max: null,
    tiger_inst_buy_min: null, tiger_inst_buy_max: null, tiger_participant: [],
    ti_ma5_min: null, ti_ma5_max: null, ti_ma10_min: null, ti_ma10_max: null,
    ti_ma20_min: null, ti_ma20_max: null, ti_ma60_min: null, ti_ma60_max: null,
    ti_ma120_min: null, ti_ma120_max: null,
    ps_min: null, ps_max: null, pcf_min: null, pcf_max: null, dtsyl_min: null, dtsyl_max: null,
    total_market_cap_min: null, total_market_cap_max: null, free_cap_min: null, free_cap_max: null,
    basic_eps_min: null, bvps_min: null, per_fcfe_min: null, parent_netprofit_min: null,
    deduct_netprofit_min: null, total_operate_income_min: null, jroa_min: null, roic_min: null,
    sale_npr_min_filter: null, debt_asset_ratio_min: null, debt_asset_ratio_max: null,
    current_ratio_min: null, speed_ratio_min: null,
    total_shares_min: null, total_shares_max: null, free_shares_min: null, free_shares_max: null,
    holder_newest_min: null, holder_newest_max: null,
    ma_30_break: [], kdj_signals: [], pattern_signals: [], consecutive_signals: [], volume_trend: [],
    net_inflow_min: null, ddx_min: null, netinflow_min_3d: null, netinflow_min_5d: null,
    changerate_3d_min: null, changerate_5d_min: null, changerate_10d_min: null,
    changerate_ty_min: null, changerate_ty_max: null,
    holder_change_3m_min: null, executive_change_3m_min: null, org_rating_filter: '',
    allcorp_ratio_min: null, allcorp_fund_ratio_min: null, allcorp_qs_ratio_min: null, allcorp_qfii_ratio_min: null,
    new_high_filter: [], win_market_filter: [], hs_board_filter: [],
    par_dividend_min: null, pledge_ratio_max: null, goodwill_max: null,
    limited_lift_filter: [], directional_seo_filter: [], equity_pledge_filter: [],
    pe_range: [0, 100], pb_range: [0, 20],
    mi_amplitude_range: [0, 50], mi_pe_range: [-200, 500],
    mi_float_mc_range: [0, 20000], mi_total_mc_range: [0, 50000],
    mi_comp_ratio_range: [-100, 100], mi_today_up_range: [-10, 10],
    mi_change_5d_range: [-50, 50], mi_change_10d_range: [-50, 50],
    mi_change_60d_range: [-100, 100], mi_change_ytd_range: [-200, 500],
    mi_close_range: [0, 2000], mi_net_in_range: [-10000000000, 10000000000],
    ch_cost_price_range: [0, 2000], ch_profit_ratio_range: [0, 100], ch_avg_cost_range: [0, 2000],
    ch_conc_90_range: [0, 100], ch_conc_70_range: [0, 100], ch_holder_count_range: [0, 2000000],
    tiger_buy_range: [0, 100000000000], tiger_sell_range: [0, 100000000000],
    tiger_net_range: [-50000000000, 50000000000], tiger_dept_buy_range: [0, 50000000000], tiger_inst_buy_range: [0, 50000000000],
    ti_ma5_range: [0, 2000], ti_ma10_range: [0, 2000], ti_ma20_range: [0, 2000],
    ti_ma60_range: [0, 2000], ti_ma120_range: [0, 2000]
  }
}

export default {
  name: 'FilterPanel',
  props: {
    filters: { type: Object, required: true }
  },
  data () {
    return {
      activeTab: 'fundamental',
      sliderConfigs: SLIDER_CONFIGS,
      localFilters: {}
    }
  },
  watch: {
    filters: {
      handler (val) {
        this.localFilters = { ...val }
      },
      immediate: true,
      deep: true
    }
  },
  methods: {
    getDefaultFilters,
    emitUpdate () {
      this.$emit('update:filters', { ...this.localFilters })
      this.$emit('change')
    },
    onSliderChange (rangeKey, val) {
      const minKey = rangeKey.replace('_range', '_min')
      const maxKey = rangeKey.replace('_range', '_max')
      this.localFilters[minKey] = val[0]
      this.localFilters[maxKey] = val[1]
      this.emitUpdate()
    }
  }
}
</script>

<style scoped>
.filter-group h4 {
  margin-bottom: 10px;
  font-size: 13px;
  color: #409eff;
  font-weight: 600;
  padding-bottom: 6px;
  border-bottom: 1px solid #ebeef5;
}

.filter-group .el-checkbox {
  margin-right: 15px;
  margin-bottom: 8px;
}

.filter-group .el-checkbox ::v-deep .el-checkbox__label {
  font-size: 12px;
}

.slider-wrapper {
  width: 35%;
  padding: 0 2px;
  flex-shrink: 0;
}

.custom-slider {
  margin: 2px 0;
}

.custom-slider ::v-deep .el-slider__runway {
  height: 4px;
}

.custom-slider ::v-deep .el-slider__bar {
  background: linear-gradient(90deg, #409eff, #66b1ff);
  height: 4px;
}

.custom-slider ::v-deep .el-slider__button {
  border-color: #409eff;
  width: 14px;
  height: 14px;
  transition: transform 0.2s ease;
}

.custom-slider ::v-deep .el-slider__button:hover {
  transform: scale(1.2);
}

.filter-group .el-form-item {
  margin-bottom: 2px;
  display: flex;
  align-items: center;
}

.filter-group .el-form-item ::v-deep .el-form-item__label {
  flex-shrink: 0;
  min-width: 90px;
  text-align: right;
  padding-right: 8px;
  line-height: 32px;
  font-size: 12px;
  color: #606266;
}

.filter-group .el-form-item ::v-deep .el-form-item__content {
  flex: 1;
  display: flex;
  align-items: center;
}

.range-input {
  display: flex;
  align-items: center;
  gap: 5px;
}

.range-min, .range-max {
  flex: 1;
  width: calc(20% - 10px);
}

.range-label {
  white-space: nowrap;
  color: #909399;
  font-size: 12px;
}
</style>
