<template>
  <div class="xuangu-container" :class="{ 'theme-dark': isDarkTheme }" :style="{ '--primary-color': primaryColor }">
    <!-- 新增一个根包装器 -->
    <div class="stock-screener-app">
      <!-- 顶部市场选择 -->
      <div class="market-filters">
        <el-radio-group v-model="selectedMarket" size="medium" @change="updateAiQuery">
          <el-radio-button label="全部"></el-radio-button>
          <el-radio-button label="A股"></el-radio-button>
          <el-radio-button label="沪深300"></el-radio-button>
          <el-radio-button label="中证500"></el-radio-button>
          <el-radio-button label="科创板"></el-radio-button>
          <el-radio-button label="创业板"></el-radio-button>
          <el-radio-button label="港股"></el-radio-button>
          <el-radio-button label="美股"></el-radio-button>
          <el-radio-button label="ETF基金"></el-radio-button>
        </el-radio-group>
      </div>

      <!-- AI选股输入框 -->
      <div class="ai-search-container">
        <el-popover
          placement="bottom-start"
          width="100%"
          trigger="click"
          v-model="filterDialogVisible"
          popper-class="filter-popover"
          :visible-arrow="false">
          <el-button slot="reference" type="info" icon="el-icon-setting" class="filter-trigger-btn">筛选条件</el-button>
          <div class="selector-panel">
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
                            <el-slider
                              v-model="filters.pe_range"
                              :min="sliderConfigs.pe.min"
                              :max="sliderConfigs.pe.max"
                              :step="sliderConfigs.pe.step"
                              range
                              @change="handleSliderChange('pe_range', $event)"
                              class="custom-slider"></el-slider>
                          </div>
                        </el-form-item>
                        <el-form-item label="PB (市净率)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.pb_range"
                              :min="sliderConfigs.pb.min"
                              :max="sliderConfigs.pb.max"
                              :step="sliderConfigs.pb.step"
                              range
                              @change="handleSliderChange('pb_range', $event)"
                              class="custom-slider"></el-slider>
                          </div>
                        </el-form-item>
                        <el-form-item label="股息率(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.dividend_min"
                              :min="0"
                              :max="20"
                              :step="0.1"
                              :format-tooltip="v => v + '%'"
                              @change="updateAiQuery"
                              class="custom-slider"></el-slider>
                          </div>
                        </el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                  <el-col :span="8">
                    <div class="filter-group">
                      <h4>成长能力</h4>
                      <el-checkbox-group v-model="filters.growth_indicators" @change="updateAiQuery">
                        <el-checkbox label="netprofit_yoy_ratio">净利增长&gt;15%</el-checkbox>
                        <el-checkbox label="toi_yoy_ratio">营收增长&gt;15%</el-checkbox>
                        <el-checkbox label="basiceps_yoy_ratio">每股收益增长&gt;10%</el-checkbox>
                        <el-checkbox label="income_growthrate_3y">营收3年复合增长 > 10%</el-checkbox>
                        <el-checkbox label="netprofit_growthrate_3y">净利润3年复合增长 > 10%</el-checkbox>
                      </el-checkbox-group>
                    </div>
                  </el-col>
                  <el-col :span="8">
                    <div class="filter-group">
                      <h4>盈利能力</h4>
                      <el-form size="mini">
                        <el-form-item label="ROE(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.roe_min"
                              :min="-50"
                              :max="100"
                              :step="1"
                              :format-tooltip="v => v + '%'"
                              @change="updateAiQuery"
                              class="custom-slider"></el-slider>
                          </div>
                        </el-form-item>
                        <el-form-item label="毛利率(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.sale_gpr_min"
                              :min="-50"
                              :max="100"
                              :step="1"
                              :format-tooltip="v => v + '%'"
                              @change="updateAiQuery"
                              class="custom-slider"></el-slider>
                          </div>
                        </el-form-item>
                        <el-form-item label="销售净利率(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.sale_npr_min"
                              :min="-100"
                              :max="100"
                              :step="1"
                              :format-tooltip="v => v + '%'"
                              @change="updateAiQuery"
                              class="custom-slider"></el-slider>
                          </div>
                        </el-form-item>
                      </el-form>
                      <el-checkbox-group v-model="filters.quality_indicators" @change="updateAiQuery">
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
                      <el-checkbox-group v-model="filters.ma_breakthrough" @change="updateAiQuery">
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
                      <el-checkbox-group v-model="filters.tech_signals" @change="updateAiQuery">
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
                      <el-checkbox-group v-model="filters.k_classic" @change="updateAiQuery">
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
                      <el-checkbox-group v-model="filters.k_intraday" @change="updateAiQuery">
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
                      <el-checkbox-group v-model="filters.k_other" @change="updateAiQuery">
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
                      <el-checkbox-group v-model="filters.capital_flow" @change="updateAiQuery">
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
                            <el-slider
                              v-model="filters.volume_ratio_min"
                              :min="0"
                              :max="50"
                              :step="0.1"
                              @change="updateAiQuery"
                              class="custom-slider"></el-slider>
                          </div>
                        </el-form-item>
                        <el-form-item label="换手率(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.turnoverrate_min"
                              :min="0"
                              :max="50"
                              :step="0.1"
                              :format-tooltip="v => v + '%'"
                              @change="updateAiQuery"
                              class="custom-slider"></el-slider>
                          </div>
                        </el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                  <el-col :span="8">
                    <div class="filter-group">
                      <h4>机构持股</h4>
                      <el-checkbox-group v-model="filters.institutional_holding" @change="updateAiQuery">
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
                      <el-select
                        v-model="filters.industry"
                        multiple
                        placeholder="请选择行业"
                        @change="updateAiQuery"
                        size="mini"
                        filterable
                      >
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
                      <el-select
                        v-model="filters.concept"
                        multiple
                        placeholder="请选择概念"
                        @change="updateAiQuery"
                        size="mini"
                        filterable
                      >
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

              <!-- ==================== 新增 Tab 5: 行情指标 ==================== -->
              <el-tab-pane label="行情指标" name="market_indicator">
                <el-row :gutter="12">
                  <el-col :span="6">
                    <div class="filter-group">
                      <h4>量价指标</h4>
                      <el-form size="mini">
                        <el-form-item label="量比">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_volume_ratio_min"
                              :min="sliderConfigs.mi_volume_ratio.min"
                              :max="sliderConfigs.mi_volume_ratio.max"
                              :step="sliderConfigs.mi_volume_ratio.step"
                              @change="updateAiQuery"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="换手率(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_turnover_rate_min"
                              :min="sliderConfigs.mi_turnover_rate.min"
                              :max="sliderConfigs.mi_turnover_rate.max"
                              :step="sliderConfigs.mi_turnover_rate.step"
                              @change="updateAiQuery"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="振幅(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_amplitude_range"
                              :min="sliderConfigs.mi_amplitude.min"
                              :max="sliderConfigs.mi_amplitude.max"
                              :step="sliderConfigs.mi_amplitude.step"
                              range
                              @change="handleSliderChange('mi_amplitude_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="成交量(手)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_volume_min"
                              :min="sliderConfigs.mi_volume.min"
                              :max="sliderConfigs.mi_volume.max"
                              :step="sliderConfigs.mi_volume.step"
                              @change="updateAiQuery"
                              class="custom-slider"></el-slider></div></el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                  <el-col :span="6">
                    <div class="filter-group">
                      <h4>估值 & 市值</h4>
                      <el-form size="mini">
                        <el-form-item label="成交额(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_amount_min"
                              :min="sliderConfigs.mi_amount.min"
                              :max="sliderConfigs.mi_amount.max"
                              :step="sliderConfigs.mi_amount.step"
                              @change="updateAiQuery"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="市盈率">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_pe_range"
                              :min="sliderConfigs.mi_pe.min"
                              :max="sliderConfigs.mi_pe.max"
                              :step="sliderConfigs.mi_pe.step"
                              range
                              @change="handleSliderChange('mi_pe_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="流通市值(亿)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_float_mc_range"
                              :min="sliderConfigs.mi_float_mc.min"
                              :max="sliderConfigs.mi_float_mc.max"
                              :step="sliderConfigs.mi_float_mc.step"
                              range
                              @change="handleSliderChange('mi_float_mc_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="总市值(亿)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_total_mc_range"
                              :min="sliderConfigs.mi_total_mc.min"
                              :max="sliderConfigs.mi_total_mc.max"
                              :step="sliderConfigs.mi_total_mc.step"
                              range
                              @change="handleSliderChange('mi_total_mc_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                  <el-col :span="6">
                    <div class="filter-group">
                      <h4>涨跌</h4>
                      <el-form size="mini">
                        <el-form-item label="委比(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_comp_ratio_range"
                              :min="sliderConfigs.mi_comp_ratio.min"
                              :max="sliderConfigs.mi_comp_ratio.max"
                              :step="sliderConfigs.mi_comp_ratio.step"
                              range
                              @change="handleSliderChange('mi_comp_ratio_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="今日涨幅(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_today_up_range"
                              :min="sliderConfigs.mi_today_up.min"
                              :max="sliderConfigs.mi_today_up.max"
                              :step="sliderConfigs.mi_today_up.step"
                              range
                              @change="handleSliderChange('mi_today_up_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="5日涨幅(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_change_5d_range"
                              :min="sliderConfigs.mi_change_5d.min"
                              :max="sliderConfigs.mi_change_5d.max"
                              :step="sliderConfigs.mi_change_5d.step"
                              range
                              @change="handleSliderChange('mi_change_5d_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="10日涨幅(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_change_10d_range"
                              :min="sliderConfigs.mi_change_10d.min"
                              :max="sliderConfigs.mi_change_10d.max"
                              :step="sliderConfigs.mi_change_10d.step"
                              range
                              @change="handleSliderChange('mi_change_10d_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                  <el-col :span="6">
                    <div class="filter-group">
                      <h4>价格 & 资金</h4>
                      <el-form size="mini">
                        <el-form-item label="60日涨幅(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_change_60d_range"
                              :min="sliderConfigs.mi_change_60d.min"
                              :max="sliderConfigs.mi_change_60d.max"
                              :step="sliderConfigs.mi_change_60d.step"
                              range
                              @change="handleSliderChange('mi_change_60d_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="年初至今(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_change_ytd_range"
                              :min="sliderConfigs.mi_change_ytd.min"
                              :max="sliderConfigs.mi_change_ytd.max"
                              :step="sliderConfigs.mi_change_ytd.step"
                              range
                              @change="handleSliderChange('mi_change_ytd_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="收盘价(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_close_range"
                              :min="sliderConfigs.mi_close.min"
                              :max="sliderConfigs.mi_close.max"
                              :step="sliderConfigs.mi_close.step"
                              range
                              @change="handleSliderChange('mi_close_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="净流入(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.mi_net_in_range"
                              :min="sliderConfigs.mi_net_in.min"
                              :max="sliderConfigs.mi_net_in.max"
                              :step="sliderConfigs.mi_net_in.step"
                              range
                              @change="handleSliderChange('mi_net_in_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                </el-row>
              </el-tab-pane>
              <!-- ==================== 新增 Tab: 特殊筛选 ==================== -->
              <el-tab-pane label="特殊筛选" name="special_filter">
                <el-row :gutter="12">
                  <el-col :span="8">
                    <div class="filter-group">
                      <h4>新高新低</h4>
                      <el-checkbox-group v-model="filters.new_high_filter" @change="updateAiQuery">
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
                      <el-checkbox-group v-model="filters.win_market_filter" @change="updateAiQuery">
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
                      <el-checkbox-group v-model="filters.consecutive_signals" @change="updateAiQuery">
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
                      <el-checkbox-group v-model="filters.limited_lift_filter" @change="updateAiQuery">
                        <el-checkbox label="limited_lift_6m">限价上涨6月</el-checkbox>
                        <el-checkbox label="limited_lift_1y">限价上涨1年</el-checkbox>
                      </el-checkbox-group>
                      <el-checkbox-group v-model="filters.directional_seo_filter" @change="updateAiQuery">
                        <el-checkbox label="directional_seo_1m">定向增发1月</el-checkbox>
                        <el-checkbox label="directional_seo_3m">定向增发3月</el-checkbox>
                        <el-checkbox label="directional_seo_6m">定向增发6月</el-checkbox>
                        <el-checkbox label="directional_seo_1y">定向增发1年</el-checkbox>
                      </el-checkbox-group>
                      <el-checkbox-group v-model="filters.equity_pledge_filter" @change="updateAiQuery">
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
                      <el-checkbox-group v-model="filters.hs_board_filter" @change="updateAiQuery">
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

              <!-- ==================== 新增 Tab 6: 筹码指标 ==================== -->
              <el-tab-pane label="筹码指标" name="chip">
                <el-row :gutter="12">
                  <el-col :span="6">
                    <div class="filter-group">
                      <h4>成本分布</h4>
                      <el-form size="mini">
                        <el-form-item label="成本价(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.ch_cost_price_range"
                              :min="sliderConfigs.ch_cost_price.min"
                              :max="sliderConfigs.ch_cost_price.max"
                              :step="sliderConfigs.ch_cost_price.step"
                              range
                              @change="handleSliderChange('ch_cost_price_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="获利比例(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.ch_profit_ratio_range"
                              :min="sliderConfigs.ch_profit_ratio.min"
                              :max="sliderConfigs.ch_profit_ratio.max"
                              :step="sliderConfigs.ch_profit_ratio.step"
                              range
                              @change="handleSliderChange('ch_profit_ratio_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="平均成本(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.ch_avg_cost_range"
                              :min="sliderConfigs.ch_avg_cost.min"
                              :max="sliderConfigs.ch_avg_cost.max"
                              :step="sliderConfigs.ch_avg_cost.step"
                              range
                              @change="handleSliderChange('ch_avg_cost_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                  <el-col :span="6">
                    <div class="filter-group">
                      <h4>集中度 & 股东</h4>
                      <el-form size="mini">
                        <el-form-item label="90%集中度(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.ch_conc_90_range"
                              :min="sliderConfigs.ch_conc_90.min"
                              :max="sliderConfigs.ch_conc_90.max"
                              :step="sliderConfigs.ch_conc_90.step"
                              range
                              @change="handleSliderChange('ch_conc_90_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="70%集中度(%)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.ch_conc_70_range"
                              :min="sliderConfigs.ch_conc_70.min"
                              :max="sliderConfigs.ch_conc_70.max"
                              :step="sliderConfigs.ch_conc_70.step"
                              range
                              @change="handleSliderChange('ch_conc_70_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="股东总数(户)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.ch_holder_count_range"
                              :min="sliderConfigs.ch_holder_count.min"
                              :max="sliderConfigs.ch_holder_count.max"
                              :step="sliderConfigs.ch_holder_count.step"
                              range
                              @change="handleSliderChange('ch_holder_count_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                </el-row>
              </el-tab-pane>

              <!-- ==================== 新增 Tab 7: 龙虎榜 ==================== -->
              <el-tab-pane label="龙虎榜" name="tiger">
                <el-row :gutter="12">
                  <el-col :span="6">
                    <div class="filter-group">
                      <h4>上榜信息</h4>
                      <el-form size="mini">
                        <el-form-item label="上榜日期">
                          <div class="range-input">
                            <el-date-picker
                              v-model="filters.tiger_date_min"
                              type="date"
                              placeholder="起始"
                              size="small"
                              value-format="yyyy-MM-dd"
                              @change="updateAiQuery"
                              class="range-min"></el-date-picker>
                            <span class="range-label">~</span>
                            <el-date-picker
                              v-model="filters.tiger_date_max"
                              type="date"
                              placeholder="截止"
                              size="small"
                              value-format="yyyy-MM-dd"
                              @change="updateAiQuery"
                              class="range-max"></el-date-picker>
                          </div>
                        </el-form-item>
                        <el-form-item label="龙虎榜买入(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.tiger_buy_range"
                              :min="sliderConfigs.tiger_buy.min"
                              :max="sliderConfigs.tiger_buy.max"
                              :step="sliderConfigs.tiger_buy.step"
                              range
                              @change="handleSliderChange('tiger_buy_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="龙虎榜卖出(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.tiger_sell_range"
                              :min="sliderConfigs.tiger_sell.min"
                              :max="sliderConfigs.tiger_sell.max"
                              :step="sliderConfigs.tiger_sell.step"
                              range
                              @change="handleSliderChange('tiger_sell_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                  <el-col :span="6">
                    <div class="filter-group">
                      <h4>净买 & 参与方</h4>
                      <el-form size="mini">
                        <el-form-item label="龙虎榜净买入(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.tiger_net_range"
                              :min="sliderConfigs.tiger_net.min"
                              :max="sliderConfigs.tiger_net.max"
                              :step="sliderConfigs.tiger_net.step"
                              range
                              @change="handleSliderChange('tiger_net_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="营业部买入(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.tiger_dept_buy_range"
                              :min="sliderConfigs.tiger_dept_buy.min"
                              :max="sliderConfigs.tiger_dept_buy.max"
                              :step="sliderConfigs.tiger_dept_buy.step"
                              range
                              @change="handleSliderChange('tiger_dept_buy_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="机构买入(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.tiger_inst_buy_range"
                              :min="sliderConfigs.tiger_inst_buy.min"
                              :max="sliderConfigs.tiger_inst_buy.max"
                              :step="sliderConfigs.tiger_inst_buy.step"
                              range
                              @change="handleSliderChange('tiger_inst_buy_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                      </el-form>
                      <el-checkbox-group v-model="filters.tiger_participant" @change="updateAiQuery">
                        <el-checkbox label="inst_participated">机构参与</el-checkbox>
                        <el-checkbox label="dept_participated">营业部参与</el-checkbox>
                      </el-checkbox-group>
                    </div>
                  </el-col>
                </el-row>
              </el-tab-pane>

              <!-- ==================== 新增 Tab 8: 技术指标(均线) ==================== -->
              <el-tab-pane label="技术指标" name="tech_indicator">
                <el-row :gutter="12">
                  <el-col :span="6">
                    <div class="filter-group">
                      <h4>均线价格</h4>
                      <el-form size="mini">
                        <el-form-item label="MA5(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.ti_ma5_range"
                              :min="sliderConfigs.ti_ma5.min"
                              :max="sliderConfigs.ti_ma5.max"
                              :step="sliderConfigs.ti_ma5.step"
                              range
                              @change="handleSliderChange('ti_ma5_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="MA10(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.ti_ma10_range"
                              :min="sliderConfigs.ti_ma10.min"
                              :max="sliderConfigs.ti_ma10.max"
                              :step="sliderConfigs.ti_ma10.step"
                              range
                              @change="handleSliderChange('ti_ma10_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                  <el-col :span="6">
                    <div class="filter-group">
                      <h4>&nbsp;</h4>
                      <el-form size="mini">
                        <el-form-item label="MA20(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.ti_ma20_range"
                              :min="sliderConfigs.ti_ma20.min"
                              :max="sliderConfigs.ti_ma20.max"
                              :step="sliderConfigs.ti_ma20.step"
                              range
                              @change="handleSliderChange('ti_ma20_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                        <el-form-item label="MA60(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.ti_ma60_range"
                              :min="sliderConfigs.ti_ma60.min"
                              :max="sliderConfigs.ti_ma60.max"
                              :step="sliderConfigs.ti_ma60.step"
                              range
                              @change="handleSliderChange('ti_ma60_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                  <el-col :span="6">
                    <div class="filter-group">
                      <h4>&nbsp;</h4>
                      <el-form size="mini">
                        <el-form-item label="MA120(元)">
                          <div class="slider-wrapper">
                            <el-slider
                              v-model="filters.ti_ma120_range"
                              :min="sliderConfigs.ti_ma120.min"
                              :max="sliderConfigs.ti_ma120.max"
                              :step="sliderConfigs.ti_ma120.step"
                              range
                              @change="handleSliderChange('ti_ma120_range', $event)"
                              class="custom-slider"></el-slider></div></el-form-item>
                      </el-form>
                    </div>
                  </el-col>
                </el-row>
              </el-tab-pane>
            </el-tabs>
          </div>
        </el-popover>
        <el-input
          v-model="aiQuery"
          type="textarea"
          :rows="2"
          placeholder="输入自然语言选股条件，如：'市盈率低于20的科技股' 或 '近5日突破60日均线的银行股'"
          class="ai-input"
          @input="handleAiInput"
        >
        </el-input>
        <div class="btn-group">
          <el-button type="primary" @click="onSearch" icon="el-icon-search">智能搜索</el-button>
          <el-button type="warning" @click="onEastmoneyPick" icon="el-icon-s-operation">东方财富选股</el-button>
          <el-button type="success" @click="openSaveDialog" icon="el-icon-star-on">保存策略</el-button>
          <el-button type="info" @click="openMyStrategies" icon="el-icon-folder-opened">我的策略</el-button>
        </div>
      </div>

      <!-- 结果表格 -->
      <div class="result-table">
        <div class="table-toolbar">
          <el-button type="warning" size="small" icon="el-icon-star-off" @click="addSelectedToFavorites" :disabled="selectedRows.length === 0">加自选 ({{ selectedRows.length }})</el-button>
        </div>
        <div class="table-scroll-wrapper">
          <el-table
            :data="tableData"
            style="width: max-content; min-width: 100%"
            :default-sort="{ prop: 'code', order: 'ascending' }"
            @sort-change="handleSortChange"
            @selection-change="handleSelectionChange"
            height="400"
          >
            <el-table-column type="selection" width="45" fixed></el-table-column>
            <!-- ========== ① 基本信息 ========== -->
            <el-table-column prop="code" label="代码" sortable="custom" width="100"></el-table-column>
            <el-table-column prop="name" label="名称" sortable="custom" width="120"></el-table-column>
            <el-table-column prop="industry" label="行业" width="100"></el-table-column>
            <el-table-column prop="concept" label="概念" show-tooltip-when-overflow width="150"></el-table-column>
            <!-- ========== ② 实时行情 ========== -->
            <el-table-column prop="new_price" label="最新价" sortable width="100"></el-table-column>
            <el-table-column prop="change_rate" label="涨跌幅(%)" sortable width="100">
              <template slot-scope="scope">
                <span :class="scope.row.change_rate >= 0 ? 'text-red' : 'text-green'">
                  {{ scope.row.change_rate && scope.row.change_rate.toFixed(2) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="high_price" label="最高价" sortable width="100"></el-table-column>
            <el-table-column prop="low_price" label="最低价" sortable width="100"></el-table-column>
            <el-table-column prop="pre_close_price" label="昨收价" sortable width="100"></el-table-column>
            <el-table-column prop="volume" label="成交量" sortable width="110"></el-table-column>
            <el-table-column prop="deal_amount" label="成交金额" sortable width="120"></el-table-column>
            <el-table-column prop="volume_ratio" label="量比" width="80"></el-table-column>
            <el-table-column prop="turnoverrate" label="换手率(%)" width="100"></el-table-column>
            <el-table-column prop="amplitude" label="振幅(%)" sortable width="90"></el-table-column>
            <!-- ========== ③ 估值指标 ========== -->
            <el-table-column prop="pe9" label="PE-TTM" width="100"></el-table-column>
            <el-table-column prop="pbnewmrq" label="PB-MRQ" width="100"></el-table-column>
            <el-table-column prop="pettmdeducted" label="扣非PE-TTM" width="110"></el-table-column>
            <el-table-column prop="ps9" label="市销率TTM" width="100"></el-table-column>
            <el-table-column prop="pcfjyxjl9" label="市现率TTM" width="100"></el-table-column>
            <el-table-column prop="predict_pe_syear" label="预测PE下年" width="110"></el-table-column>
            <el-table-column prop="predict_pe_nyear" label="预测PE后两年" width="120"></el-table-column>
            <el-table-column prop="dtsyl" label="动态市盈率" width="100"></el-table-column>
            <el-table-column prop="ycpeg" label="预测市盈率" width="100"></el-table-column>
            <el-table-column prop="enterprise_value_multiple" label="企业价值倍数" width="120"></el-table-column>
            <el-table-column prop="total_market_cap" label="总市值" sortable width="120"></el-table-column>
            <el-table-column prop="free_cap" label="流通市值" sortable width="120"></el-table-column>
            <!-- ========== ④ 每股指标 ========== -->
            <el-table-column prop="basic_eps" label="每股收益" width="90"></el-table-column>
            <el-table-column prop="bvps" label="每股净资产" width="100"></el-table-column>
            <el-table-column prop="per_netcash_operate" label="每股经营现金流" width="120"></el-table-column>
            <el-table-column prop="per_fcfe" label="每股自由现金流" width="120"></el-table-column>
            <el-table-column prop="per_capital_reserve" label="每股资本公积" width="110"></el-table-column>
            <el-table-column prop="per_unassign_profit" label="每股未分配利润" width="120"></el-table-column>
            <el-table-column prop="per_surplus_reserve" label="每股盈余公积" width="110"></el-table-column>
            <el-table-column prop="per_retained_earning" label="每股留存收益" width="110"></el-table-column>
            <!-- ========== ⑤ 利润与营收 ========== -->
            <el-table-column prop="parent_netprofit" label="归母净利润" sortable width="120"></el-table-column>
            <el-table-column prop="deduct_netprofit" label="扣非净利润" sortable width="120"></el-table-column>
            <el-table-column prop="total_operate_income" label="营业收入" sortable width="120"></el-table-column>
            <el-table-column prop="roe_weight" label="加权ROE(%)" width="100"></el-table-column>
            <el-table-column prop="jroa" label="总资产报酬率(%)" width="120"></el-table-column>
            <el-table-column prop="roic" label="投资资本回报率(%)" width="130"></el-table-column>
            <el-table-column prop="sale_gpr" label="销售毛利率(%)" width="120"></el-table-column>
            <el-table-column prop="sale_npr" label="销售净利率(%)" width="110"></el-table-column>
            <el-table-column prop="zxgxl" label="新增股本" width="90"></el-table-column>
            <!-- ========== ⑥ 成长能力 ========== -->
            <el-table-column prop="netprofit_yoy_ratio" label="净利润同比增长(%)" width="130"></el-table-column>
            <el-table-column prop="deduct_netprofit_growthrate" label="扣非净利增长率(%)" width="130"></el-table-column>
            <el-table-column prop="toi_yoy_ratio" label="营收同比增长(%)" width="120"></el-table-column>
            <el-table-column prop="netprofit_growthrate_3y" label="净利3年复合增长(%)" width="140"></el-table-column>
            <el-table-column prop="income_growthrate_3y" label="营收3年复合增长(%)" width="140"></el-table-column>
            <el-table-column prop="predict_netprofit_ratio" label="预测净利润比率(%)" width="130"></el-table-column>
            <el-table-column prop="predict_income_ratio" label="预测营收比率(%)" width="120"></el-table-column>
            <el-table-column prop="basiceps_yoy_ratio" label="EPS同比增长(%)" width="120"></el-table-column>
            <el-table-column prop="total_profit_growthrate" label="总利润增长率(%)" width="120"></el-table-column>
            <el-table-column prop="operate_profit_growthrate" label="营业利润增长率(%)" width="130"></el-table-column>
            <!-- ========== ⑦ 财务健康 ========== -->
            <el-table-column prop="debt_asset_ratio" label="资产负债率(%)" width="120"></el-table-column>
            <el-table-column prop="equity_ratio" label="权益比率" width="90"></el-table-column>
            <el-table-column prop="equity_multiplier" label="权益乘数" width="90"></el-table-column>
            <el-table-column prop="current_ratio" label="流动比率" width="90"></el-table-column>
            <el-table-column prop="speed_ratio" label="速动比率" width="90"></el-table-column>
            <!-- ========== ⑧ 股东与机构 ========== -->
            <el-table-column prop="total_shares" label="总股本" sortable width="100"></el-table-column>
            <el-table-column prop="free_shares" label="流通股本" sortable width="100"></el-table-column>
            <el-table-column prop="holder_newest" label="最新股东数" width="100"></el-table-column>
            <el-table-column prop="holder_ratio" label="股东比例" width="90"></el-table-column>
            <el-table-column prop="hold_amount" label="持仓金额" width="100"></el-table-column>
            <el-table-column prop="avg_hold_num" label="平均持仓数量" width="110"></el-table-column>
            <el-table-column prop="holdnum_growthrate_3q" label="持仓3季增长率(%)" width="130"></el-table-column>
            <el-table-column prop="holdnum_growthrate_hy" label="持仓半年增长率(%)" width="130"></el-table-column>
            <el-table-column prop="hold_ratio_count" label="持股比例" width="90"></el-table-column>
            <el-table-column prop="free_hold_ratio" label="自由流通持股比例" width="130"></el-table-column>
            <el-table-column prop="holder_change_3m" label="持股变动3月(%)" width="120"></el-table-column>
            <el-table-column prop="executive_change_3m" label="高管持股变动3月(%)" width="140"></el-table-column>
            <el-table-column prop="org_rating" label="机构评级" width="90"></el-table-column>
            <el-table-column prop="allcorp_num" label="全部机构数" width="100"></el-table-column>
            <el-table-column prop="allcorp_fund_num" label="基金公司数" width="100"></el-table-column>
            <el-table-column prop="allcorp_qs_num" label="券商公司数" width="100"></el-table-column>
            <el-table-column prop="allcorp_qfii_num" label="QFII公司数" width="100"></el-table-column>
            <el-table-column prop="allcorp_bx_num" label="保险公司数" width="100"></el-table-column>
            <el-table-column prop="allcorp_sb_num" label="社保公司数" width="100"></el-table-column>
            <el-table-column prop="allcorp_xt_num" label="信托公司数" width="100"></el-table-column>
            <el-table-column prop="allcorp_ratio" label="机构持股比例(%)" width="130"></el-table-column>
            <el-table-column prop="allcorp_fund_ratio" label="基金持股比例(%)" width="120"></el-table-column>
            <el-table-column prop="allcorp_qs_ratio" label="券商持股比例(%)" width="120"></el-table-column>
            <el-table-column prop="allcorp_qfii_ratio" label="QFII持股比例(%)" width="120"></el-table-column>
            <el-table-column prop="allcorp_bx_ratio" label="保险持股比例(%)" width="120"></el-table-column>
            <el-table-column prop="allcorp_sb_ratio" label="社保持股比例(%)" width="120"></el-table-column>
            <el-table-column prop="allcorp_xt_ratio" label="信托持股比例(%)" width="120"></el-table-column>
            <!-- ========== ⑨ 技术指标 ========== -->
            <el-table-column prop="short_avg_array" label="短期均线多头" width="110">
              <template slot-scope="scope">
                <span :class="scope.row.short_avg_array ? 'text-red' : 'text-grey'">{{ scope.row.short_avg_array ? '是' : '否' }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="breakup_ma_30days" label="突破30日均线" width="110">
              <template slot-scope="scope">
                <span :class="scope.row.breakup_ma_30days ? 'text-red' : ''">{{ scope.row.breakup_ma_30days ? '是' : '否' }}</span>
              </template>
            </el-table-column>

          </el-table>
        </div>

        <!-- 分页 -->
        <div class="pagination-container">
          <el-pagination
            @size-change="handleSizeChange"
            @current-change="handleCurrentChange"
            :current-page="currentPage"
            :page-sizes="[10, 20, 50, 100]"
            :page-size="pageSize"
            layout="total, sizes, prev, pager, next, jumper"
            :total="totalItems"
          >
          </el-pagination>
        </div>
      </div>

      <!-- ===== 保存策略对话框 ===== -->
      <el-dialog title="保存选股策略" :visible.sync="saveDialogVisible" width="420px">
        <el-form :model="saveForm" label-width="70px">
          <el-form-item label="名称">
            <el-input v-model="saveForm.name" placeholder="如：低估值银行股" maxlength="100" show-word-limit></el-input>
          </el-form-item>
          <el-form-item label="描述">
            <el-input v-model="saveForm.description" type="textarea" :rows="2" placeholder="可选" maxlength="500"></el-input>
          </el-form-item>
          <el-form-item label="条件">
            <div class="strategy-conditions-preview">
              {{ aiQuery || '（无筛选条件）' }}
            </div>
          </el-form-item>
        </el-form>
        <span slot="footer">
          <el-button @click="saveDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="doSaveStrategy" :loading="saving">保存</el-button>
        </span>
      </el-dialog>

      <!-- ===== 我的策略对话框 ===== -->
      <el-dialog title="我的策略" :visible.sync="strategiesDialogVisible" width="600px">
        <div v-if="strategiesLoading" style="text-align:center;padding:20px;">
          <i class="el-icon-loading" style="font-size:24px;"></i>
        </div>
        <div v-else-if="myStrategies.length === 0" style="text-align:center;color:#909399;padding:30px;">
          暂无保存的策略
        </div>
        <el-table v-else :data="myStrategies" style="width:100%" :show-header="false" size="small">
          <el-table-column prop="name" label="名称" width="160">
            <template slot-scope="{ row }">
              <strong>{{ row.name }}</strong>
              <div style="color:#909399;font-size:12px;">{{ row.updated_at }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="description" label="描述">
            <template slot-scope="{ row }">
              <div style="color:#606266;font-size:13px;">{{ row.description || '—' }}</div>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="160" align="right">
            <template slot-scope="{ row }">
              <el-button size="mini" type="primary" @click="loadStrategy(row)">加载</el-button>
              <el-button size="mini" type="danger" @click="deleteStrategy(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <span slot="footer">
          <el-button @click="strategiesDialogVisible = false">关闭</el-button>
        </span>
      </el-dialog>

    </div>
  </div>
</template>

<script>
import Vue from 'vue'
import ElementUI from 'element-ui'
import 'element-ui/lib/theme-chalk/index.css'

Vue.config.productionTip = false
Vue.use(ElementUI)

export default {
  name: 'StockScreener',
  data () {
    return {
      selectedMarket: '全部',
      aiQuery: '', // 初始化为空
      activeTab: 'fundamental',
      filterDialogVisible: false,

      // 核心修复：添加标志位，防止死循环
      isUpdatingFromAi: false,

      filters: {
        // 基本面
        pe_min: null,
        pe_max: null,
        pb_min: null,
        pb_max: null,
        dividend_min: null,
        growth_indicators: [],
        quality_indicators: [],
        roe_min: null,
        sale_gpr_min: null,
        sale_npr_min: null,

        // 技术面
        ma_breakthrough: [],
        tech_signals: [],
        // 技术面 - K线形态（拆分为三组）
        k_classic: [],
        k_intraday: [],
        k_other: [],

        // 资金面
        capital_flow: [],
        volume_ratio_min: null,
        turnoverrate_min: null,
        institutional_holding: [],

        // 概念行业
        industry: [],
        concept: [],

        // ========== 行情指标 ==========
        mi_volume_ratio_min: null,
        mi_volume_ratio_max: null,
        mi_turnover_rate_min: null,
        mi_turnover_rate_max: null,
        mi_amplitude_min: null,
        mi_amplitude_max: null,
        mi_pe_min: null,
        mi_pe_max: null,
        mi_float_mc_min: null,
        mi_float_mc_max: null,
        mi_total_mc_min: null,
        mi_total_mc_max: null,
        mi_comp_ratio_min: null,
        mi_comp_ratio_max: null,
        mi_volume_min: null,
        mi_volume_max: null,
        mi_amount_min: null,
        mi_amount_max: null,
        mi_net_in_min: null,
        mi_net_in_max: null,
        mi_today_up_min: null,
        mi_today_up_max: null,
        mi_change_5d_min: null,
        mi_change_5d_max: null,
        mi_change_10d_min: null,
        mi_change_10d_max: null,
        mi_change_60d_min: null,
        mi_change_60d_max: null,
        mi_change_ytd_min: null,
        mi_change_ytd_max: null,
        mi_close_min: null,
        mi_close_max: null,

        // ========== 筹码指标 ==========
        ch_cost_price_min: null,
        ch_cost_price_max: null,
        ch_profit_ratio_min: null,
        ch_profit_ratio_max: null,
        ch_conc_90_min: null,
        ch_conc_90_max: null,
        ch_conc_70_min: null,
        ch_conc_70_max: null,
        ch_avg_cost_min: null,
        ch_avg_cost_max: null,
        ch_holder_count_min: null,
        ch_holder_count_max: null,

        // ========== 龙虎榜 ==========
        tiger_date_min: null,
        tiger_date_max: null,
        tiger_buy_min: null,
        tiger_buy_max: null,
        tiger_sell_min: null,
        tiger_sell_max: null,
        tiger_net_min: null,
        tiger_net_max: null,
        tiger_dept_buy_min: null,
        tiger_dept_buy_max: null,
        tiger_inst_buy_min: null,
        tiger_inst_buy_max: null,
        tiger_participant: [],

        // ========== 技术指标(均线) ==========
        ti_ma5_min: null,
        ti_ma5_max: null,
        ti_ma10_min: null,
        ti_ma10_max: null,
        ti_ma20_min: null,
        ti_ma20_max: null,
        ti_ma60_min: null,
        ti_ma60_max: null,
        ti_ma120_min: null,
        ti_ma120_max: null,

        // ========== 新增基本面筛选 ==========
        ps_min: null,
        ps_max: null,
        pcf_min: null,
        pcf_max: null,
        dtsyl_min: null,
        dtsyl_max: null,
        total_market_cap_min: null,
        total_market_cap_max: null,
        free_cap_min: null,
        free_cap_max: null,
        basic_eps_min: null,
        bvps_min: null,
        per_fcfe_min: null,
        parent_netprofit_min: null,
        deduct_netprofit_min: null,
        total_operate_income_min: null,
        jroa_min: null,
        roic_min: null,
        sale_npr_min_filter: null,
        debt_asset_ratio_min: null,
        debt_asset_ratio_max: null,
        current_ratio_min: null,
        speed_ratio_min: null,
        total_shares_min: null,
        total_shares_max: null,
        free_shares_min: null,
        free_shares_max: null,
        holder_newest_min: null,
        holder_newest_max: null,

        // ========== 新增技术面筛选 ==========
        ma_30_break: [],
        kdj_signals: [],
        pattern_signals: [],
        consecutive_signals: [],
        volume_trend: [],

        // ========== 新增资金面筛选 ==========
        net_inflow_min: null,
        ddx_min: null,
        netinflow_min_3d: null,
        netinflow_min_5d: null,
        changerate_3d_min: null,
        changerate_5d_min: null,
        changerate_10d_min: null,
        changerate_ty_min: null,
        changerate_ty_max: null,

        // ========== 新增股东机构筛选 ==========
        holder_change_3m_min: null,
        executive_change_3m_min: null,
        org_rating_filter: '',
        allcorp_ratio_min: null,
        allcorp_fund_ratio_min: null,
        allcorp_qs_ratio_min: null,
        allcorp_qfii_ratio_min: null,

        // ========== 新增状态筛选 ==========
        new_high_filter: [],
        win_market_filter: [],
        hs_board_filter: [],

        // ========== 新增派息与质押筛选 ==========
        par_dividend_min: null,
        pledge_ratio_max: null,
        goodwill_max: null,

        // ========== 新增限价/定增/质押时间筛选 ==========
        limited_lift_filter: [],
        directional_seo_filter: [],
        equity_pledge_filter: [],

        // ========== 滑块绑定值（数组 [min, max]）==========
        pe_range: [0, 100],
        pb_range: [0, 20],
        mi_amplitude_range: [0, 50],
        mi_pe_range: [-200, 500],
        mi_float_mc_range: [0, 20000],
        mi_total_mc_range: [0, 50000],
        mi_comp_ratio_range: [-100, 100],
        mi_today_up_range: [-10, 10],
        mi_change_5d_range: [-50, 50],
        mi_change_10d_range: [-50, 50],
        mi_change_60d_range: [-100, 100],
        mi_change_ytd_range: [-200, 500],
        mi_close_range: [0, 2000],
        mi_net_in_range: [-10000000000, 10000000000],
        ch_cost_price_range: [0, 2000],
        ch_profit_ratio_range: [0, 100],
        ch_avg_cost_range: [0, 2000],
        ch_conc_90_range: [0, 100],
        ch_conc_70_range: [0, 100],
        ch_holder_count_range: [0, 2000000],
        tiger_buy_range: [0, 100000000000],
        tiger_sell_range: [0, 100000000000],
        tiger_net_range: [-50000000000, 50000000000],
        tiger_dept_buy_range: [0, 50000000000],
        tiger_inst_buy_range: [0, 50000000000],
        ti_ma5_range: [0, 2000],
        ti_ma10_range: [0, 2000],
        ti_ma20_range: [0, 2000],
        ti_ma60_range: [0, 2000],
        ti_ma120_range: [0, 2000]
      },

      // ========== 滑块配置 ==========
      sliderConfigs: {
        // 基本面
        pe: { min: -100, max: 500, step: 1, defaults: [0, 100] },
        pb: { min: -50, max: 100, step: 0.1, defaults: [0, 20] },
        // 行情指标
        mi_volume_ratio: { min: 0, max: 100, step: 0.1, defaults: [0, 100] },
        mi_turnover_rate: { min: 0, max: 100, step: 0.1, defaults: [0, 100] },
        mi_amplitude: { min: 0, max: 50, step: 0.1, defaults: [0, 50] },
        mi_volume: { min: 0, max: 10000000, step: 10000, defaults: [0, 10000000] },
        mi_amount: { min: 0, max: 500000000000, step: 100000000, defaults: [0, 500000000000] },
        mi_pe: { min: -200, max: 500, step: 1, defaults: [-200, 500] },
        mi_float_mc: { min: 0, max: 20000, step: 10, defaults: [0, 20000] },
        mi_total_mc: { min: 0, max: 50000, step: 10, defaults: [0, 50000] },
        mi_comp_ratio: { min: -100, max: 100, step: 1, defaults: [-100, 100] },
        mi_today_up: { min: -10, max: 10, step: 0.1, defaults: [-10, 10] },
        mi_change_5d: { min: -50, max: 50, step: 0.1, defaults: [-50, 50] },
        mi_change_10d: { min: -50, max: 50, step: 0.1, defaults: [-50, 50] },
        mi_change_60d: { min: -100, max: 100, step: 0.1, defaults: [-100, 100] },
        mi_change_ytd: { min: -200, max: 500, step: 0.1, defaults: [-200, 500] },
        mi_close: { min: 0, max: 2000, step: 0.01, defaults: [0, 2000] },
        mi_net_in: { min: -10000000000, max: 10000000000, step: 100000000, defaults: [-10000000000, 10000000000] },
        // 筹码指标
        ch_cost_price: { min: 0, max: 2000, step: 0.01, defaults: [0, 2000] },
        ch_profit_ratio: { min: 0, max: 100, step: 0.1, defaults: [0, 100] },
        ch_avg_cost: { min: 0, max: 2000, step: 0.01, defaults: [0, 2000] },
        ch_conc_90: { min: 0, max: 100, step: 0.1, defaults: [0, 100] },
        ch_conc_70: { min: 0, max: 100, step: 0.1, defaults: [0, 100] },
        ch_holder_count: { min: 0, max: 2000000, step: 10000, defaults: [0, 2000000] },
        // 龙虎榜
        tiger_buy: { min: 0, max: 100000000000, step: 100000000, defaults: [0, 100000000000] },
        tiger_sell: { min: 0, max: 100000000000, step: 100000000, defaults: [0, 100000000000] },
        tiger_net: { min: -50000000000, max: 50000000000, step: 100000000, defaults: [-50000000000, 50000000000] },
        tiger_dept_buy: { min: 0, max: 50000000000, step: 100000000, defaults: [0, 50000000000] },
        tiger_inst_buy: { min: 0, max: 50000000000, step: 100000000, defaults: [0, 50000000000] },
        // 技术指标(均线)
        ti_ma5: { min: 0, max: 2000, step: 0.01, defaults: [0, 2000] },
        ti_ma10: { min: 0, max: 2000, step: 0.01, defaults: [0, 2000] },
        ti_ma20: { min: 0, max: 2000, step: 0.01, defaults: [0, 2000] },
        ti_ma60: { min: 0, max: 2000, step: 0.01, defaults: [0, 2000] },
        ti_ma120: { min: 0, max: 2000, step: 0.01, defaults: [0, 2000] }
      },

      // 表格数据
      tableData: [],
      selectedRows: [],
      currentPage: 1,
      pageSize: 50,
      totalItems: 0,

      // 用于缓存上次更新AI查询的时间戳，防止过于频繁的更新
      lastUpdateTimestamp: 0,

      // 策略管理
      saveDialogVisible: false,
      saveForm: { name: '', description: '' },
      saving: false,
      strategiesDialogVisible: false,
      strategiesLoading: false,
      myStrategies: []
    }
  },
  methods: {
    // 将前端参数转换为后端可接受的SQL WHERE条件
    buildQueryConditions () {
      const conditions = []
      const params = { }

      // 市场选择
      if (this.selectedMarket !== '全部') {
        if (this.selectedMarket === '沪深300') {
          conditions.push('is_hs300 = "Y"') // 假设数据库中用 Y/N 表示
        } else if (this.selectedMarket === '中证500') {
          conditions.push('is_zz500 = "Y"')
        } else {
          // A股、港股、美股等，假设用market字段区分
          conditions.push('market = :market')
          params.market = this.selectedMarket
        }
      }

      // 关键词搜索 (代码/名称/概念)
      if (this.aiQuery.trim()) {
        conditions.push('(code LIKE :keyword OR name LIKE :keyword OR concept LIKE :keyword)')
        params.keyword = `%${this.aiQuery.trim()}%`
      }

      // 基本面条件
      if (this.filters.pe_min !== null) {
        conditions.push('pe9 >= :pe_min')
        params.pe_min = this.filters.pe_min
      }
      if (this.filters.pe_max !== null) {
        conditions.push('pe9 <= :pe_max')
        params.pe_max = this.filters.pe_max
      }
      if (this.filters.pb_min !== null) {
        conditions.push('pbnewmrq >= :pb_min')
        params.pb_min = this.filters.pb_min
      }
      if (this.filters.pb_max !== null) {
        conditions.push('pbnewmrq <= :pb_max')
        params.pb_max = this.filters.pb_max
      }
      if (this.filters.dividend_min !== null) {
        conditions.push('par_dividend >= :dividend_min')
        params.dividend_min = this.filters.dividend_min
      }
      if (this.filters.roe_min !== null) {
        conditions.push('roe_weight >= :roe_min')
        params.roe_min = this.filters.roe_min
      }
      if (this.filters.sale_gpr_min !== null) {
        conditions.push('sale_gpr >= :sale_gpr_min')
        params.sale_gpr_min = this.filters.sale_gpr_min
      }
      // 处理成长性布尔字段 (假设数据库中是数值字段，大于0表示满足条件)
      this.filters.growth_indicators.forEach((indicator) => {
        if (indicator === 'netprofit_yoy_ratio') {
          conditions.push('netprofit_yoy_ratio > 15') // 示例阈值
        } else if (indicator === 'toi_yoy_ratio') {
          conditions.push('toi_yoy_ratio > 15')
        } else if (indicator === 'basiceps_yoy_ratio') {
          conditions.push('basiceps_yoy_ratio > 10')
        }
      })
      // 处理质量性布尔字段
      this.filters.quality_indicators.forEach((indicator) => {
        if (indicator === 'per_netcash_operate') {
          conditions.push('per_netcash_operate > 0')
        }
      })

      // 技术面条件 (布尔字段)
      this.filters.ma_breakthrough.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.tech_signals.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.k_classic.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.k_intraday.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.k_other.forEach((field) => {
        conditions.push(`${field} = 1`)
      })

      // 资金面条件
      if (this.filters.volume_ratio_min !== null) {
        conditions.push('volume_ratio >= :volume_ratio_min')
        params.volume_ratio_min = this.filters.volume_ratio_min
      }
      if (this.filters.turnoverrate_min !== null) {
        conditions.push('turnoverrate >= :turnoverrate_min')
        params.turnoverrate_min = this.filters.turnoverrate_min
      }
      // 行情指标 - 单端下限
      if (this.filters.mi_volume_ratio_min !== null && this.filters.mi_volume_ratio_min > 0) {
        conditions.push('volume_ratio >= :mi_volume_ratio_min')
        params.mi_volume_ratio_min = this.filters.mi_volume_ratio_min
      }
      if (this.filters.mi_turnover_rate_min !== null && this.filters.mi_turnover_rate_min > 0) {
        conditions.push('turnoverrate >= :mi_turnover_rate_min')
        params.mi_turnover_rate_min = this.filters.mi_turnover_rate_min
      }
      if (this.filters.mi_volume_min !== null && this.filters.mi_volume_min > 0) {
        conditions.push('volume >= :mi_volume_min')
        params.mi_volume_min = this.filters.mi_volume_min
      }
      if (this.filters.mi_amount_min !== null && this.filters.mi_amount_min > 0) {
        conditions.push('deal_amount >= :mi_amount_min')
        params.mi_amount_min = this.filters.mi_amount_min
      }
      this.filters.capital_flow.forEach((field) => {
        if (field === 'low_funds_inflow') {
          conditions.push('net_inflow > 0') // 假设 net_inflow > 0 表示净流入
        } else if (field === 'netinflow_3days') {
          conditions.push('netinflow_3days > 0')
        }
      })
      this.filters.institutional_holding.forEach((field) => {
        if (field === 'org_survey_3m') {
          conditions.push('org_survey_3m > 0')
        } else if (field === 'allcorp_fund_ratio') {
          conditions.push('allcorp_fund_ratio > 0.05') // 示例阈值
        }
      })

      // 概念行业
      if (this.filters.industry.length > 0) {
        const placeholders = this.filters.industry.map((_, i) => `:industry${i}`).join(', ')
        conditions.push(`industry IN (${placeholders})`)
        this.filters.industry.forEach((val, i) => {
          params[`industry${i}`] = val
        })
      }
      if (this.filters.concept.length > 0) {
        // 概念字段通常是文本，可能包含多个概念，用逗号分隔
        this.filters.concept.forEach((val, i) => {
          conditions.push(`concept LIKE :concept${i}`)
          params[`concept${i}`] = `%${val}%`
        })
      }

      // ====== 新增基本面条件 ======
      if (this.filters.ps_min !== null) {
        conditions.push('ps9 >= :ps_min')
        params.ps_min = this.filters.ps_min
      }
      if (this.filters.ps_max !== null) {
        conditions.push('ps9 <= :ps_max')
        params.ps_max = this.filters.ps_max
      }
      if (this.filters.pcf_min !== null) {
        conditions.push('pcfjyxjl9 >= :pcf_min')
        params.pcf_min = this.filters.pcf_min
      }
      if (this.filters.pcf_max !== null) {
        conditions.push('pcfjyxjl9 <= :pcf_max')
        params.pcf_max = this.filters.pcf_max
      }
      if (this.filters.dtsyl_min !== null) {
        conditions.push('dtsyl >= :dtsyl_min')
        params.dtsyl_min = this.filters.dtsyl_min
      }
      if (this.filters.dtsyl_max !== null) {
        conditions.push('dtsyl <= :dtsyl_max')
        params.dtsyl_max = this.filters.dtsyl_max
      }
      if (this.filters.total_market_cap_min !== null) {
        conditions.push('total_market_cap >= :total_market_cap_min')
        params.total_market_cap_min = this.filters.total_market_cap_min
      }
      if (this.filters.total_market_cap_max !== null) {
        conditions.push('total_market_cap <= :total_market_cap_max')
        params.total_market_cap_max = this.filters.total_market_cap_max
      }
      if (this.filters.free_cap_min !== null) {
        conditions.push('free_cap >= :free_cap_min')
        params.free_cap_min = this.filters.free_cap_min
      }
      if (this.filters.free_cap_max !== null) {
        conditions.push('free_cap <= :free_cap_max')
        params.free_cap_max = this.filters.free_cap_max
      }
      if (this.filters.basic_eps_min !== null) {
        conditions.push('basic_eps >= :basic_eps_min')
        params.basic_eps_min = this.filters.basic_eps_min
      }
      if (this.filters.bvps_min !== null) {
        conditions.push('bvps >= :bvps_min')
        params.bvps_min = this.filters.bvps_min
      }
      if (this.filters.per_fcfe_min !== null) {
        conditions.push('per_fcfe >= :per_fcfe_min')
        params.per_fcfe_min = this.filters.per_fcfe_min
      }
      if (this.filters.parent_netprofit_min !== null) {
        conditions.push('parent_netprofit >= :parent_netprofit_min')
        params.parent_netprofit_min = this.filters.parent_netprofit_min
      }
      if (this.filters.deduct_netprofit_min !== null) {
        conditions.push('deduct_netprofit >= :deduct_netprofit_min')
        params.deduct_netprofit_min = this.filters.deduct_netprofit_min
      }
      if (this.filters.total_operate_income_min !== null) {
        conditions.push('total_operate_income >= :total_operate_income_min')
        params.total_operate_income_min = this.filters.total_operate_income_min
      }
      if (this.filters.jroa_min !== null) {
        conditions.push('jroa >= :jroa_min')
        params.jroa_min = this.filters.jroa_min
      }
      if (this.filters.roic_min !== null) {
        conditions.push('roic >= :roic_min')
        params.roic_min = this.filters.roic_min
      }
      if (this.filters.sale_npr_min_filter !== null) {
        conditions.push('sale_npr >= :sale_npr_min_filter')
        params.sale_npr_min_filter = this.filters.sale_npr_min_filter
      }
      if (this.filters.debt_asset_ratio_max !== null) {
        conditions.push('debt_asset_ratio <= :debt_asset_ratio_max')
        params.debt_asset_ratio_max = this.filters.debt_asset_ratio_max
      }
      if (this.filters.current_ratio_min !== null) {
        conditions.push('current_ratio >= :current_ratio_min')
        params.current_ratio_min = this.filters.current_ratio_min
      }
      if (this.filters.speed_ratio_min !== null) {
        conditions.push('speed_ratio >= :speed_ratio_min')
        params.speed_ratio_min = this.filters.speed_ratio_min
      }
      if (this.filters.total_shares_min !== null) {
        conditions.push('total_shares >= :total_shares_min')
        params.total_shares_min = this.filters.total_shares_min
      }
      if (this.filters.total_shares_max !== null) {
        conditions.push('total_shares <= :total_shares_max')
        params.total_shares_max = this.filters.total_shares_max
      }
      if (this.filters.free_shares_min !== null) {
        conditions.push('free_shares >= :free_shares_min')
        params.free_shares_min = this.filters.free_shares_min
      }
      if (this.filters.free_shares_max !== null) {
        conditions.push('free_shares <= :free_shares_max')
        params.free_shares_max = this.filters.free_shares_max
      }
      if (this.filters.holder_newest_min !== null) {
        conditions.push('holder_newest >= :holder_newest_min')
        params.holder_newest_min = this.filters.holder_newest_min
      }
      if (this.filters.holder_newest_max !== null) {
        conditions.push('holder_newest <= :holder_newest_max')
        params.holder_newest_max = this.filters.holder_newest_max
      }

      // ====== 新增技术面条件 ======
      this.filters.ma_30_break.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.kdj_signals.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.pattern_signals.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.consecutive_signals.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.volume_trend.forEach((field) => {
        conditions.push(`${field} = 1`)
      })

      // ====== 新增资金面条件 ======
      if (this.filters.net_inflow_min !== null) {
        conditions.push('net_inflow >= :net_inflow_min')
        params.net_inflow_min = this.filters.net_inflow_min
      }
      if (this.filters.ddx_min !== null) {
        conditions.push('ddx >= :ddx_min')
        params.ddx_min = this.filters.ddx_min
      }
      if (this.filters.netinflow_min_3d !== null) {
        conditions.push('netinflow_3days >= :netinflow_min_3d')
        params.netinflow_min_3d = this.filters.netinflow_min_3d
      }
      if (this.filters.netinflow_min_5d !== null) {
        conditions.push('netinflow_5days >= :netinflow_min_5d')
        params.netinflow_min_5d = this.filters.netinflow_min_5d
      }
      if (this.filters.changerate_3d_min !== null) {
        conditions.push('changerate_3days >= :changerate_3d_min')
        params.changerate_3d_min = this.filters.changerate_3d_min
      }
      if (this.filters.changerate_5d_min !== null) {
        conditions.push('changerate_5days >= :changerate_5d_min')
        params.changerate_5d_min = this.filters.changerate_5d_min
      }
      if (this.filters.changerate_10d_min !== null) {
        conditions.push('changerate_10days >= :changerate_10d_min')
        params.changerate_10d_min = this.filters.changerate_10d_min
      }
      if (this.filters.changerate_ty_min !== null) {
        conditions.push('changerate_ty >= :changerate_ty_min')
        params.changerate_ty_min = this.filters.changerate_ty_min
      }
      if (this.filters.changerate_ty_max !== null) {
        conditions.push('changerate_ty <= :changerate_ty_max')
        params.changerate_ty_max = this.filters.changerate_ty_max
      }

      // ====== 新增股东机构条件 ======
      if (this.filters.holder_change_3m_min !== null) {
        conditions.push('holder_change_3m >= :holder_change_3m_min')
        params.holder_change_3m_min = this.filters.holder_change_3m_min
      }
      if (this.filters.executive_change_3m_min !== null) {
        conditions.push('executive_change_3m >= :executive_change_3m_min')
        params.executive_change_3m_min = this.filters.executive_change_3m_min
      }
      if (this.filters.org_rating_filter) {
        conditions.push('org_rating = :org_rating_filter')
        params.org_rating_filter = this.filters.org_rating_filter
      }
      if (this.filters.allcorp_ratio_min !== null) {
        conditions.push('allcorp_ratio >= :allcorp_ratio_min')
        params.allcorp_ratio_min = this.filters.allcorp_ratio_min
      }
      if (this.filters.allcorp_fund_ratio_min !== null) {
        conditions.push('allcorp_fund_ratio >= :allcorp_fund_ratio_min')
        params.allcorp_fund_ratio_min = this.filters.allcorp_fund_ratio_min
      }
      if (this.filters.allcorp_qs_ratio_min !== null) {
        conditions.push('allcorp_qs_ratio >= :allcorp_qs_ratio_min')
        params.allcorp_qs_ratio_min = this.filters.allcorp_qs_ratio_min
      }
      if (this.filters.allcorp_qfii_ratio_min !== null) {
        conditions.push('allcorp_qfii_ratio >= :allcorp_qfii_ratio_min')
        params.allcorp_qfii_ratio_min = this.filters.allcorp_qfii_ratio_min
      }

      // ====== 新增状态条件 ======
      this.filters.new_high_filter.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.win_market_filter.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.hs_board_filter.forEach((field) => {
        if (field === 'is_sz50') conditions.push('is_sz50 = "Y"')
        if (field === 'is_zz1000') conditions.push('is_zz1000 = "Y"')
        if (field === 'is_cy50') conditions.push('is_cy50 = "Y"')
        if (field === 'is_issue_break') conditions.push('is_issue_break = 1')
        if (field === 'is_bps_break') conditions.push('is_bps_break = 1')
      })

      // ====== 新增派息与质押条件 ======
      if (this.filters.par_dividend_min !== null) {
        conditions.push('par_dividend >= :par_dividend_min')
        params.par_dividend_min = this.filters.par_dividend_min
      }
      if (this.filters.pledge_ratio_max !== null) {
        conditions.push('pledge_ratio <= :pledge_ratio_max')
        params.pledge_ratio_max = this.filters.pledge_ratio_max
      }
      if (this.filters.goodwill_max !== null) {
        conditions.push('goodwill_scale <= :goodwill_max')
        params.goodwill_max = this.filters.goodwill_max
      }

      // ====== 新增限价/定增/质押时间条件 ======
      this.filters.limited_lift_filter.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.directional_seo_filter.forEach((field) => {
        conditions.push(`${field} = 1`)
      })
      this.filters.equity_pledge_filter.forEach((field) => {
        conditions.push(`${field} = 1`)
      })

      return {
        whereClause: conditions.length > 0 ? 'WHERE ' + conditions.join(' AND ') : '',
        params: params
      }
    },

    // 搜索按钮
    async onSearch () {
      this.currentPage = 1
      await this.performSearch()
    },

    // 东方财富选股
    onEastmoneyPick () {
      const inputValue = encodeURIComponent(this.aiQuery || '')
      window.open(`https://xuangu.eastmoney.com/Result?j=question_rec&id=xc10dd2addee07009398&inputValue=${inputValue}`, '_blank')
    },

    // ---- 获取认证 headers ----
    _authHeaders () {
      const token = localStorage.getItem('token')
      const h = { 'Content-Type': 'application/json' }
      if (token) h['Authorization'] = `Bearer ${token}`
      return h
    },

    // ====== 策略保存/加载/删除 ======

    openSaveDialog () {
      const { whereClause } = this.buildQueryConditions()
      if (!whereClause) {
        this.$message.warning('请先设置筛选条件再保存')
        return
      }
      this.saveForm = { name: '', description: '' }
      this.saveDialogVisible = true
    },

    async doSaveStrategy () {
      if (!this.saveForm.name.trim()) {
        this.$message.warning('请输入策略名称')
        return
      }

      const { whereClause, params } = this.buildQueryConditions()
      this.saving = true

      try {
        const resp = await fetch('/api/xuangu/favorites', {
          method: 'POST',
          headers: this._authHeaders(),
          body: JSON.stringify({
            name: this.saveForm.name.trim(),
            conditions: { query: whereClause, params },
            description: this.saveForm.description.trim()
          })
        })
        const data = await resp.json()
        if (data.code === 0) {
          this.$message.success(data.msg || '保存成功')
          this.saveDialogVisible = false
        } else {
          this.$message.error(data.msg || '保存失败')
        }
      } catch (e) {
        this.$message.error('保存失败: ' + e.message)
      } finally {
        this.saving = false
      }
    },

    async openMyStrategies () {
      this.strategiesDialogVisible = true
      this.strategiesLoading = true
      try {
        const resp = await fetch('/api/xuangu/favorites', {
          method: 'GET',
          headers: this._authHeaders()
        })
        const data = await resp.json()
        this.myStrategies = data.code === 0 ? (data.data || []) : []
      } catch (e) {
        this.$message.error('加载失败: ' + e.message)
      } finally {
        this.strategiesLoading = false
      }
    },

    loadStrategy (strategy) {
      // conditions 格式: { query: "WHERE ...", params: {...} }
      let cond = strategy.conditions
      if (typeof cond === 'string') {
        try { cond = JSON.parse(cond) } catch (_) { cond = {} }
      }

      // 恢复筛选条件到 aiQuery 输入框 → 触发 handleAiInput 解析
      // 但条件是结构化的，直接用 WHERE 子句 + params 填充更快
      // 这里用简单方式：把条件对象转回 aiQuery 文本
      if (cond && cond.query) {
        // 直接设置搜索参数并执行
        this._pendingQuery = cond
        this.aiQuery = ''
        this.strategiesDialogVisible = false
        // 用条件直接搜索
        this._searchWithDirectConditions(cond)
      } else {
        this.$message.warning('策略条件格式不兼容')
      }
    },

    async _searchWithDirectConditions (cond) {
      this.currentPage = 1
      try {
        const resp = await fetch('/api/xuangu/search', {
          method: 'POST',
          headers: this._authHeaders(),
          body: JSON.stringify({
            query: cond.query || '',
            params: cond.params || {},
            page: 1,
            limit: this.pageSize,
            sort_by: 'code',
            order: 'asc'
          })
        })
        const data = await resp.json()
        if (data.code === 0) {
          this.tableData = data.data
          this.totalItems = data.count
          this.$message.success(`已加载策略，${data.count} 条结果`)
        } else {
          this.$message.error(data.msg || '加载策略失败')
        }
      } catch (e) {
        this.$message.error('加载策略失败: ' + e.message)
      }
    },

    async deleteStrategy (strategy) {
      try {
        await this.$confirm(`确定删除策略「${strategy.name}」？`, '确认删除', {
          type: 'warning'
        })
      } catch (_) {
        return
      }

      try {
        const resp = await fetch(`/api/xuangu/favorites/${strategy.id}`, {
          method: 'DELETE',
          headers: this._authHeaders()
        })
        const data = await resp.json()
        if (data.code === 0) {
          this.$message.success('已删除')
          this.myStrategies = this.myStrategies.filter(s => s.id !== strategy.id)
        } else {
          this.$message.error(data.msg || '删除失败')
        }
      } catch (e) {
        this.$message.error('删除失败: ' + e.message)
      }
    },

    // 执行搜索逻辑
    async performSearch () {
      try {
        const { whereClause, params } = this.buildQueryConditions()

        const response = await fetch('/api/xuangu/search', {
          method: 'POST',
          headers: this._authHeaders(),
          body: JSON.stringify({
            query: whereClause, // 将WHERE子句传递给后端
            params: params, // 将参数对象传递给后端
            page: this.currentPage,
            limit: this.pageSize,
            'sort_by': 'code', // 默认排序
            order: 'asc'
          })
        })

        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`)
        }
        const responseData = await response.json()

        if (responseData.code === 0) {
          this.tableData = responseData.data
          this.totalItems = responseData.count
        } else {
          // 处理后端返回的非成功状态码
          this.$message.error(responseData.message || '搜索失败')
        }
      } catch (error) {
        console.error('搜索失败:', error)
        this.$message.error('搜索失败，请检查网络或稍后重试')
      }
    },

    // 解析数字（支持万/亿后缀）
    parseNumber (str) {
      if (!str) return null
      str = String(str).replace(/,/g, '').trim()
      if (str === '∞' || str === '-∞') return null
      if (str.endsWith('千亿')) return parseFloat(str) * 100000000000
      if (str.endsWith('亿')) return parseFloat(str) * 100000000
      if (str.endsWith('万')) return parseFloat(str) * 10000
      if (str.endsWith('手')) return parseFloat(str)
      if (str.endsWith('元')) return parseFloat(str)
      if (str.endsWith('%')) return parseFloat(str)
      return parseFloat(str)
    },

    // 重置所有筛选条件
    resetAllFilters () {
      const nullFields = [
        'pe_min', 'pe_max', 'pb_min', 'pb_max', 'dividend_min', 'roe_min', 'sale_gpr_min', 'sale_npr_min',
        'volume_ratio_min', 'turnoverrate_min',
        'ps_min', 'ps_max', 'pcf_min', 'pcf_max', 'dtsyl_min', 'dtsyl_max',
        'total_market_cap_min', 'total_market_cap_max', 'free_cap_min', 'free_cap_max',
        'basic_eps_min', 'bvps_min', 'per_fcfe_min',
        'parent_netprofit_min', 'deduct_netprofit_min', 'total_operate_income_min',
        'jroa_min', 'roic_min', 'sale_npr_min_filter',
        'debt_asset_ratio_min', 'debt_asset_ratio_max',
        'current_ratio_min', 'speed_ratio_min',
        'total_shares_min', 'total_shares_max', 'free_shares_min', 'free_shares_max',
        'holder_newest_min', 'holder_newest_max',
        'net_inflow_min', 'ddx_min', 'netinflow_min_3d', 'netinflow_min_5d',
        'changerate_3d_min', 'changerate_5d_min', 'changerate_10d_min',
        'changerate_ty_min', 'changerate_ty_max',
        'holder_change_3m_min', 'executive_change_3m_min', 'allcorp_ratio_min',
        'allcorp_fund_ratio_min', 'allcorp_qs_ratio_min', 'allcorp_qfii_ratio_min',
        'par_dividend_min', 'pledge_ratio_max', 'goodwill_max',
        'mi_volume_ratio_min', 'mi_turnover_rate_min', 'mi_amplitude_min',
        'mi_volume_min', 'mi_amount_min',
        'mi_pe_min', 'mi_pe_max', 'mi_float_mc_min', 'mi_float_mc_max',
        'mi_total_mc_min', 'mi_total_mc_max',
        'mi_comp_ratio_min', 'mi_comp_ratio_max',
        'mi_today_up_min', 'mi_today_up_max',
        'mi_change_5d_min', 'mi_change_5d_max',
        'mi_change_10d_min', 'mi_change_10d_max',
        'mi_change_60d_min', 'mi_change_60d_max',
        'mi_change_ytd_min', 'mi_change_ytd_max',
        'mi_close_min', 'mi_close_max',
        'mi_net_in_min', 'mi_net_in_max',
        'ch_cost_price_min', 'ch_cost_price_max',
        'ch_profit_ratio_min', 'ch_profit_ratio_max',
        'ch_avg_cost_min', 'ch_avg_cost_max',
        'ch_conc_90_min', 'ch_conc_90_max',
        'ch_conc_70_min', 'ch_conc_70_max',
        'ch_holder_count_min', 'ch_holder_count_max',
        'tiger_date_min', 'tiger_date_max',
        'tiger_buy_min', 'tiger_buy_max', 'tiger_sell_min', 'tiger_sell_max',
        'tiger_net_min', 'tiger_net_max',
        'tiger_dept_buy_min', 'tiger_dept_buy_max',
        'tiger_inst_buy_min', 'tiger_inst_buy_max',
        'ti_ma5_min', 'ti_ma5_max', 'ti_ma10_min', 'ti_ma10_max',
        'ti_ma20_min', 'ti_ma20_max', 'ti_ma60_min', 'ti_ma60_max',
        'ti_ma120_min', 'ti_ma120_max',
        'org_rating_filter'
      ]
      nullFields.forEach(f => { this.filters[f] = null })

      // 重置市场选择为"全部"（确保输入框删除市场关键字时，市场栏也回退）
      this.selectedMarket = '全部'

      const emptyArrayFields = [
        'growth_indicators', 'quality_indicators',
        'ma_breakthrough', 'tech_signals', 'k_classic', 'k_intraday', 'k_other',
        'capital_flow', 'institutional_holding',
        'industry', 'concept',
        'new_high_filter', 'win_market_filter', 'hs_board_filter',
        'consecutive_signals', 'limited_lift_filter', 'directional_seo_filter', 'equity_pledge_filter',
        'ma_30_break', 'kdj_signals', 'pattern_signals', 'volume_trend',
        'tiger_participant'
      ]
      emptyArrayFields.forEach(f => { this.filters[f] = [] })
    },

    // 从搜索框文本解析出所有筛选条件
    parseFilterFromText (text) {
      if (!text || !text.trim()) return
      const parts = text.split(/[;；]/).map(s => s.trim()).filter(Boolean)

      // 辅助：提取 X~Y 格式的范围
      const parseRange = (str) => {
        const p = str.split('~')
        return { min: this.parseNumber(p[0]), max: p[1] ? this.parseNumber(p[1]) : null }
      }

      for (const part of parts) {
        let m

        // ---- 市场选择 ----
        if (/^(全部|A股|沪深300|中证500|科创板|创业板|港股|美股|ETF基金)$/.test(part)) {
          this.selectedMarket = part
          continue
        }

        // ---- 范围: PE在X到Y之间 ----
        if ((m = part.match(/PE在(.+?)到(.+?)之间/))) {
          this.filters.pe_min = this.parseNumber(m[1]); this.filters.pe_max = this.parseNumber(m[2]); continue
        }
        if ((m = part.match(/PB在(.+?)到(.+?)之间/))) {
          this.filters.pb_min = this.parseNumber(m[1]); this.filters.pb_max = this.parseNumber(m[2]); continue
        }

        // ---- 不低于 ----
        if ((m = part.match(/股息率不低于(.+?)%/))) { this.filters.dividend_min = parseFloat(m[1]); continue }
        if ((m = part.match(/ROE不低于(.+?)%/))) { this.filters.roe_min = parseFloat(m[1]); continue }
        if ((m = part.match(/毛利率不低于(.+?)%/))) { this.filters.sale_gpr_min = parseFloat(m[1]); continue }
        if ((m = part.match(/量比不低于(.+)/))) { this.filters.volume_ratio_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/换手率不低于(.+?)%/))) { this.filters.turnoverrate_min = parseFloat(m[1]); continue }

        // ---- 成长/质量 checkbox ----
        if (part === '净利增长>15%') { this.filters.growth_indicators.push('netprofit_yoy_ratio'); continue }
        if (part === '营收增长>15%') { this.filters.growth_indicators.push('toi_yoy_ratio'); continue }
        if (part === '每股收益增长>10%') { this.filters.growth_indicators.push('basiceps_yoy_ratio'); continue }
        if (part === '经营现金流为正') { this.filters.quality_indicators.push('per_netcash_operate'); continue }

        // ---- 均线突破 ----
        if (part === '突破5日线') { this.filters.ma_breakthrough.push('breakup_ma_5days'); continue }
        if (part === '突破10日线') { this.filters.ma_breakthrough.push('breakup_ma_10days'); continue }
        if (part === '突破20日线') { this.filters.ma_breakthrough.push('breakup_ma_20days'); continue }
        if (part === '突破60日线') { this.filters.ma_breakthrough.push('breakup_ma_60days'); continue }
        if (part === '突破30日线') { this.filters.ma_30_break.push('breakup_ma_30days'); continue }
        if (part === '长期均线多头排列') { this.filters.ma_breakthrough.push('long_avg_array'); continue }

        // ---- 技术指标 ----
        if (part === 'MACD金叉') { this.filters.tech_signals.push('macd_golden_fork'); continue }
        if (part === 'KDJ金叉') { this.filters.tech_signals.push('kdj_golden_fork'); continue }
        if (part === '放量上涨') { this.filters.tech_signals.push('upper_large_volume'); continue }
        if (part === '缩量下跌') { this.filters.tech_signals.push('down_narrow_volume'); continue }
        if (part === '突破形态') { this.filters.tech_signals.push('break_through'); continue }
        if (part === 'MACD金叉Z') { this.filters.kdj_signals.push('macd_golden_forkz'); continue }
        if (part === 'MACD金叉Y') { this.filters.kdj_signals.push('macd_golden_forky'); continue }
        if (part === 'KDJ金叉Z') { this.filters.kdj_signals.push('kdj_golden_forkz'); continue }
        if (part === 'KDJ金叉Y') { this.filters.kdj_signals.push('kdj_golden_forky'); continue }

        // ---- K线形态 ----
        const kClassicMap = { '大阳线': 'one_dayang_line', '两阳夹一阴': 'two_dayang_lines', '阳包阴': 'rise_sun', '早晨之星': 'morning_star', '黄昏之星': 'evening_star', '射击之星': 'shooting_star', '三只乌鸦': 'three_black_crows', '锤头': 'hammer', '倒锤头': 'inverted_hammer', '十字星': 'doji', '长腿十字线': 'long_legged_doji', '墓碑线': 'gravestone', '蜻蜓线': 'dragonfly', '双飞乌鸦': 'two_flying_crows', '出水芙蓉': 'lotus_emerge', '低开高走': 'low_open_high', '巨量': 'huge_volume', '底部十字孕线': 'bottom_cross_harami', '顶部十字孕线': 'top_cross_harami' }
        if (kClassicMap[part]) { this.filters.k_classic.push(kClassicMap[part]); continue }

        const kIntradayMap = { '尾盘拉升': 'tail_plate_rise', '盘中打压': 'intraday_pressure', '盘中拉升': 'intraday_rise', '快速反弹': 'quick_rebound' }
        if (kIntradayMap[part]) { this.filters.k_intraday.push(kIntradayMap[part]); continue }

        const kOtherMap = { '一字涨停': 'limit_up', '一字跌停': 'limit_down' }
        if (kOtherMap[part]) { this.filters.k_other.push(kOtherMap[part]); continue }

        // ---- pattern_signals ----
        const patternMap = { '乌云盖顶': 'power_fulgun', '孕线': 'pregnant', '黑云压顶': 'black_cloud_tops', '窄幅整理': 'narrow_finish', '反转锤子': 'reversing_hammer', '第一天黎明': 'first_dawn', '看跌吞没': 'bearish_engulfing', '上攻放量': 'upside_volume', '天道法则': 'heaven_rule' }
        if (patternMap[part]) { this.filters.pattern_signals.push(patternMap[part]); continue }

        // ---- 资金面 ----
        if (part === '主力资金净流入') { this.filters.capital_flow.push('low_funds_inflow'); continue }
        if (part === '主力资金净流出') { this.filters.capital_flow.push('high_funds_outflow'); continue }
        if (part === '近3日资金净流入') { this.filters.capital_flow.push('netinflow_3days'); continue }
        if (part === '近5日资金净流入') { this.filters.capital_flow.push('netinflow_5days'); continue }
        if (part === '近3月有机构调研') { this.filters.institutional_holding.push('org_survey_3m'); continue }
        if (part === '基金重仓') { this.filters.institutional_holding.push('allcorp_fund_ratio'); continue }
        if (part === '券商重仓') { this.filters.institutional_holding.push('allcorp_qs_ratio'); continue }
        if (part === '机构参与') { this.filters.tiger_participant.push('inst_participated'); continue }
        if (part === '营业部参与') { this.filters.tiger_participant.push('dept_participated'); continue }

        // ---- 概念/行业 ----
        if ((m = part.match(/属于行业\((.+)\)/))) { this.filters.industry = m[1].split(', '); continue }
        if ((m = part.match(/涉及概念\((.+)\)/))) { this.filters.concept = m[1].split(', '); continue }

        // ---- 新高新低 ----
        const highLowMap = { '当前新高': 'now_newhigh', '当前新低': 'now_newlow', '3天新高': 'high_recent_3days', '5天新高': 'high_recent_5days', '10天新高': 'high_recent_10days', '20天新高': 'high_recent_20days', '30天新高': 'high_recent_30days', '3天新低': 'low_recent_3days', '5天新低': 'low_recent_5days', '10天新低': 'low_recent_10days', '20天新低': 'low_recent_20days', '30天新低': 'low_recent_30days' }
        if (highLowMap[part]) { this.filters.new_high_filter.push(highLowMap[part]); continue }

        // ---- 战胜大盘 ----
        if ((m = part.match(/(\d+)天战胜大盘/))) { this.filters.win_market_filter.push(`win_market_${m[1]}days`); continue }

        // ---- 连涨连跌 ----
        const consecMap = { '连续4天上涨': 'upper_4days', '连续8天上涨': 'upper_8days', '连续9天上涨': 'upper_9days', '连续7天下跌': 'down_7days' }
        if (consecMap[part]) { this.filters.consecutive_signals.push(consecMap[part]); continue }

        // ---- 限价/定增/质押 ----
        if (part === '限价上涨6月') { this.filters.limited_lift_filter.push('limited_lift_6m'); continue }
        if (part === '限价上涨1年') { this.filters.limited_lift_filter.push('limited_lift_1y'); continue }
        if ((m = part.match(/定向增发(\d+[月年])/))) {
          const map = { '1月': 'directional_seo_1m', '3月': 'directional_seo_3m', '6月': 'directional_seo_6m', '1年': 'directional_seo_1y' }
          if (map[m[1]]) this.filters.directional_seo_filter.push(map[m[1]])
          continue
        }
        if ((m = part.match(/股权质押(\d+[月年])/))) {
          const map = { '1月': 'equity_pledge_1m', '3月': 'equity_pledge_3m', '6月': 'equity_pledge_6m', '1年': 'equity_pledge_1y' }
          if (map[m[1]]) this.filters.equity_pledge_filter.push(map[m[1]])
          continue
        }

        // ---- 板块标识 ----
        const boardMap = { '上证50成分股': 'is_sz50', '中证1000成分股': 'is_zz1000', '创业板50成分股': 'is_cy50', '已破净': 'is_bps_break', '已破板': 'is_issue_break' }
        if (boardMap[part]) { this.filters.hs_board_filter.push(boardMap[part]); continue }

        // ---- 基本面 ≥/≤ ----
        if ((m = part.match(/每股收益≥(.+)/))) { this.filters.basic_eps_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/每股净资产≥(.+)/))) { this.filters.bvps_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/每股自由现金流≥(.+)/))) { this.filters.per_fcfe_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/归母净利润≥(.+)/))) { this.filters.parent_netprofit_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/扣非净利润≥(.+)/))) { this.filters.deduct_netprofit_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/营业收入≥(.+)/))) { this.filters.total_operate_income_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/总资产报酬率≥(.+?)%/))) { this.filters.jroa_min = parseFloat(m[1]); continue }
        if ((m = part.match(/投资回报率≥(.+?)%/))) { this.filters.roic_min = parseFloat(m[1]); continue }
        if ((m = part.match(/销售净利率≥(.+?)%/))) { this.filters.sale_npr_min_filter = parseFloat(m[1]); continue }
        if ((m = part.match(/资产负债率≤(.+?)%/))) { this.filters.debt_asset_ratio_max = parseFloat(m[1]); continue }
        if ((m = part.match(/资产负债率(.+?)~(.+?)%/))) { this.filters.debt_asset_ratio_min = this.parseNumber(m[1]); this.filters.debt_asset_ratio_max = this.parseNumber(m[2]); continue }
        if ((m = part.match(/流动比率≥(.+)/))) { this.filters.current_ratio_min = parseFloat(m[1]); continue }
        if ((m = part.match(/速动比率≥(.+)/))) { this.filters.speed_ratio_min = parseFloat(m[1]); continue }
        if ((m = part.match(/派息率≥(.+?)%/))) { this.filters.par_dividend_min = parseFloat(m[1]); continue }
        if ((m = part.match(/质押比例≤(.+?)%/))) { this.filters.pledge_ratio_max = parseFloat(m[1]); continue }
        if ((m = part.match(/商誉≤(.+)/))) { this.filters.goodwill_max = this.parseNumber(m[1]); continue }

        // ---- 机构股东 ≥ ----
        if ((m = part.match(/3月持股变动≥(.+?)%/))) { this.filters.holder_change_3m_min = parseFloat(m[1]); continue }
        if ((m = part.match(/3月高管持股变动≥(.+?)%/))) { this.filters.executive_change_3m_min = parseFloat(m[1]); continue }
        if ((m = part.match(/机构评级=(.+)/))) { this.filters.org_rating_filter = m[1]; continue }
        if ((m = part.match(/机构持股比例≥(.+?)%/))) { this.filters.allcorp_ratio_min = parseFloat(m[1]); continue }
        if ((m = part.match(/基金持股≥(.+?)%/))) { this.filters.allcorp_fund_ratio_min = parseFloat(m[1]); continue }
        if ((m = part.match(/券商持股≥(.+?)%/))) { this.filters.allcorp_qs_ratio_min = parseFloat(m[1]); continue }
        if ((m = part.match(/QFII持股≥(.+?)%/))) { this.filters.allcorp_qfii_ratio_min = parseFloat(m[1]); continue }

        // ---- 资金面数值 ≥ ----
        if ((m = part.match(/净流入≥(.+)/))) { this.filters.net_inflow_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/大单动向≥(.+)/))) { this.filters.ddx_min = parseFloat(m[1]); continue }
        if ((m = part.match(/3日净流入≥(.+)/))) { this.filters.netinflow_min_3d = this.parseNumber(m[1]); continue }
        if ((m = part.match(/5日净流入≥(.+)/))) { this.filters.netinflow_min_5d = this.parseNumber(m[1]); continue }
        if ((m = part.match(/3日涨幅≥(.+?)%/))) { this.filters.changerate_3d_min = parseFloat(m[1]); continue }
        if ((m = part.match(/5日涨幅≥(.+?)%/))) { this.filters.changerate_5d_min = parseFloat(m[1]); continue }
        if ((m = part.match(/10日涨幅≥(.+?)%/))) { this.filters.changerate_10d_min = parseFloat(m[1]); continue }

        // ---- 行情指标 ≥ ----
        if ((m = part.match(/量比≥(.+)/))) { this.filters.mi_volume_ratio_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/换手率≥(.+?)%/))) { this.filters.mi_turnover_rate_min = parseFloat(m[1]); continue }
        if ((m = part.match(/成交量≥(.+?)手/))) { this.filters.mi_volume_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/成交额≥(.+)/))) { this.filters.mi_amount_min = this.parseNumber(m[1]); continue }

        // ---- 范围格式: 总市值X~Y / 流通市值X~Y 等 ----
        if ((m = part.match(/总市值(.+)/))) { const r = parseRange(m[1]); this.filters.total_market_cap_min = r.min; this.filters.total_market_cap_max = r.max; continue }
        if ((m = part.match(/流通市值(.+)/))) { const r = parseRange(m[1]); this.filters.free_cap_min = r.min; this.filters.free_cap_max = r.max; continue }
        if ((m = part.match(/市销率(.+)/))) { const r = parseRange(m[1]); this.filters.ps_min = r.min; this.filters.ps_max = r.max; continue }
        if ((m = part.match(/市现率(.+)/))) { const r = parseRange(m[1]); this.filters.pcf_min = r.min; this.filters.pcf_max = r.max; continue }
        if ((m = part.match(/动态PE(.+)/))) { const r = parseRange(m[1]); this.filters.dtsyl_min = r.min; this.filters.dtsyl_max = r.max; continue }
        if ((m = part.match(/总股本(.+)/))) { const r = parseRange(m[1]); this.filters.total_shares_min = r.min; this.filters.total_shares_max = r.max; continue }
        if ((m = part.match(/流通股本(.+)/))) { const r = parseRange(m[1]); this.filters.free_shares_min = r.min; this.filters.free_shares_max = r.max; continue }
        if ((m = part.match(/股东数(.+)/))) { const r = parseRange(m[1]); this.filters.holder_newest_min = r.min; this.filters.holder_newest_max = r.max; continue }
        if ((m = part.match(/年度涨幅(.+?)%/))) { const p = m[1].split('~'); this.filters.changerate_ty_min = this.parseNumber(p[0]); this.filters.changerate_ty_max = p[1] ? this.parseNumber(p[1]) : null; continue }

        // ---- 自由输入文字 → 关键词搜索 ----
        if (part.length > 0 && !part.includes('≥') && !part.includes('≤') && !part.includes('之间')) {
          // 纯文字部分作为关键词（和原aiQuery拼接逻辑兼容）
        }
      }
    },

    // 处理AI输入框事件（搜索框 ↔ 参数双向同步）
    handleAiInput (value) {
      this.isUpdatingFromAi = true
      // 先重置所有筛选参数
      this.resetAllFilters()
      // 再从文本重新解析
      this.parseFilterFromText(value)
      setTimeout(() => { this.isUpdatingFromAi = false }, 100)
    },

    // 核心修复2：更新AI查询框
    updateAiQuery () {
      // 如果当前是AI框触发的更新，直接返回，防止死循环
      if (this.isUpdatingFromAi) {
        return
      }

      const now = Date.now()
      if (now - this.lastUpdateTimestamp < 200) {
        return
      }
      this.lastUpdateTimestamp = now

      // 构建描述性的自然语言字符串
      const parts = []

      // 市场
      if (this.selectedMarket !== '全部') {
        parts.push(this.selectedMarket)
      }

      // 基本面
      if (this.filters.pe_min !== null || this.filters.pe_max !== null) {
        const peDesc = `PE在${this.filters.pe_min || 0}到${this.filters.pe_max || '无穷大' }之间`
        parts.push(peDesc)
      }
      if (this.filters.pb_min !== null || this.filters.pb_max !== null) {
        const pbDesc = `PB在${this.filters.pb_min || 0}到${this.filters.pb_max || '无穷大' }之间`
        parts.push(pbDesc)
      }
      if (this.filters.dividend_min !== null && this.filters.dividend_min > 0) {
        parts.push(`股息率不低于${this.filters.dividend_min}%`)
      }
      if (this.filters.roe_min !== null && this.filters.roe_min > -50) {
        parts.push(`ROE不低于${this.filters.roe_min}%`)
      }
      if (this.filters.sale_gpr_min !== null && this.filters.sale_gpr_min > -50) {
        parts.push(`毛利率不低于${this.filters.sale_gpr_min}%`)
      }
      this.filters.growth_indicators.forEach((indicator) => {
        if (indicator === 'netprofit_yoy_ratio') parts.push('净利增长>15%')
        if (indicator === 'toi_yoy_ratio') parts.push('营收增长>15%')
        if (indicator === 'basiceps_yoy_ratio') parts.push('每股收益增长>10%')
        if (indicator === 'income_growthrate_3y') parts.push('营收3年复合增长 > 10%')
        if (indicator === 'netprofit_growthrate_3y') parts.push('净利润3年复合增长 > 10%')
      })
      this.filters.quality_indicators.forEach((indicator) => {
        if (indicator === 'per_netcash_operate') parts.push('经营现金流为正')
      })

      // 技术面
      this.filters.ma_breakthrough.forEach((field) => {
        if (field === 'breakup_ma_5days') parts.push('突破5日线')
        if (field === 'breakup_ma_10days') parts.push('突破10日线')
        if (field === 'breakup_ma_20days') parts.push('突破20日线')
        if (field === 'breakup_ma_60days') parts.push('突破60日线')
        if (field === 'long_avg_array') parts.push('长期均线多头排列')
      })
      this.filters.tech_signals.forEach((field) => {
        if (field === 'macd_golden_fork') parts.push('MACD金叉')
        if (field === 'kdj_golden_fork') parts.push('KDJ金叉')
        if (field === 'upper_large_volume') parts.push('放量上涨')
        if (field === 'down_narrow_volume') parts.push('缩量下跌')
        if (field === 'break_through') parts.push('突破形态')
      })
      this.filters.k_classic.forEach((field) => {
        if (field === 'one_dayang_line') parts.push('大阳线')
        if (field === 'two_dayang_lines') parts.push('两阳夹一阴')
        if (field === 'rise_sun') parts.push('阳包阴')
        if (field === 'morning_star') parts.push('早晨之星')
        if (field === 'evening_star') parts.push('黄昏之星')
        if (field === 'shooting_star') parts.push('射击之星')
        if (field === 'three_black_crows') parts.push('三只乌鸦')
        if (field === 'hammer') parts.push('锤头')
        if (field === 'inverted_hammer') parts.push('倒锤头')
        if (field === 'doji') parts.push('十字星')
        if (field === 'long_legged_doji') parts.push('长腿十字线')
        if (field === 'gravestone') parts.push('墓碑线')
        if (field === 'dragonfly') parts.push('蜻蜓线')
        if (field === 'two_flying_crows') parts.push('双飞乌鸦')
        if (field === 'lotus_emerge') parts.push('出水芙蓉')
        if (field === 'low_open_high') parts.push('低开高走')
        if (field === 'huge_volume') parts.push('巨量')
        if (field === 'bottom_cross_harami') parts.push('底部十字孕线')
        if (field === 'top_cross_harami') parts.push('顶部十字孕线')
      })
      this.filters.k_intraday.forEach((field) => {
        if (field === 'tail_plate_rise') parts.push('尾盘拉升')
        if (field === 'intraday_pressure') parts.push('盘中打压')
        if (field === 'intraday_rise') parts.push('盘中拉升')
        if (field === 'quick_rebound') parts.push('快速反弹')
      })
      this.filters.k_other.forEach((field) => {
        if (field === 'limit_up') parts.push('一字涨停')
        if (field === 'limit_down') parts.push('一字跌停')
      })

      // 资金面
      this.filters.capital_flow.forEach((field) => {
        if (field === 'low_funds_inflow') parts.push('主力资金净流入')
        if (field === 'high_funds_outflow') parts.push('主力资金净流出')
        if (field === 'netinflow_3days') parts.push('近3日资金净流入')
        if (field === 'netinflow_5days') parts.push('近5日资金净流入')
      })
      if (this.filters.volume_ratio_min !== null && this.filters.volume_ratio_min > 0) {
        parts.push(`量比不低于${this.filters.volume_ratio_min}`)
      }
      if (this.filters.turnoverrate_min !== null && this.filters.turnoverrate_min > 0) {
        parts.push(`换手率不低于${this.filters.turnoverrate_min}%`)
      }
      // 行情指标 - 单端下限
      if (this.filters.mi_volume_ratio_min !== null && this.filters.mi_volume_ratio_min > 0) {
        parts.push(`量比≥${this.filters.mi_volume_ratio_min}`)
      }
      if (this.filters.mi_turnover_rate_min !== null && this.filters.mi_turnover_rate_min > 0) {
        parts.push(`换手率≥${this.filters.mi_turnover_rate_min}%`)
      }
      if (this.filters.mi_volume_min !== null && this.filters.mi_volume_min > 0) {
        parts.push(`成交量≥${this.filters.mi_volume_min}手`)
      }
      if (this.filters.mi_amount_min !== null && this.filters.mi_amount_min > 0) {
        parts.push(`成交额≥${this.filters.mi_amount_min}元`)
      }
      this.filters.institutional_holding.forEach((field) => {
        if (field === 'org_survey_3m') parts.push('近3月有机构调研')
        if (field === 'allcorp_fund_ratio') parts.push('基金重仓')
        if (field === 'allcorp_qs_ratio') parts.push('券商重仓')
      })
      this.filters.tiger_participant.forEach((field) => {
        if (field === 'inst_participated') parts.push('机构参与')
        if (field === 'dept_participated') parts.push('营业部参与')
      })

      // 概念行业
      if (this.filters.industry.length > 0) {
        parts.push(`属于行业(${this.filters.industry.join(', ')})`)
      }
      if (this.filters.concept.length > 0) {
        parts.push(`涉及概念(${this.filters.concept.join(', ')})`)
      }

      // ====== 新增基本面描述 ======
      if (this.filters.ps_min !== null || this.filters.ps_max !== null) {
        parts.push(`市销率${this.filters.ps_min || 0}~${this.filters.ps_max || '∞' }`)
      }
      if (this.filters.pcf_min !== null || this.filters.pcf_max !== null) {
        parts.push(`市现率${this.filters.pcf_min || 0}~${this.filters.pcf_max || '∞' }`)
      }
      if (this.filters.dtsyl_min !== null || this.filters.dtsyl_max !== null) {
        parts.push(`动态PE${this.filters.dtsyl_min || 0}~${this.filters.dtsyl_max || '∞' }`)
      }
      if (this.filters.total_market_cap_min !== null || this.filters.total_market_cap_max !== null) {
        parts.push(`总市值${this.filters.total_market_cap_min || 0}~${this.filters.total_market_cap_max || '∞' }`)
      }
      if (this.filters.free_cap_min !== null || this.filters.free_cap_max !== null) {
        parts.push(`流通市值${this.filters.free_cap_min || 0}~${this.filters.free_cap_max || '∞' }`)
      }
      if (this.filters.basic_eps_min !== null) parts.push(`每股收益≥${this.filters.basic_eps_min}`)
      if (this.filters.bvps_min !== null) parts.push(`每股净资产≥${this.filters.bvps_min}`)
      if (this.filters.per_fcfe_min !== null) parts.push(`每股自由现金流≥${this.filters.per_fcfe_min}`)
      if (this.filters.parent_netprofit_min !== null) parts.push(`归母净利润≥${this.filters.parent_netprofit_min}`)
      if (this.filters.deduct_netprofit_min !== null) parts.push(`扣非净利润≥${this.filters.deduct_netprofit_min}`)
      if (this.filters.total_operate_income_min !== null) parts.push(`营业收入≥${this.filters.total_operate_income_min}`)
      if (this.filters.jroa_min !== null) parts.push(`总资产报酬率≥${this.filters.jroa_min}%`)
      if (this.filters.roic_min !== null) parts.push(`投资回报率≥${this.filters.roic_min}%`)
      if (this.filters.sale_npr_min_filter !== null) parts.push(`销售净利率≥${this.filters.sale_npr_min_filter}%`)
      if (this.filters.debt_asset_ratio_max !== null) parts.push(`资产负债率≤${this.filters.debt_asset_ratio_max}%`)
      if (this.filters.current_ratio_min !== null) parts.push(`流动比率≥${this.filters.current_ratio_min}`)
      if (this.filters.speed_ratio_min !== null) parts.push(`速动比率≥${this.filters.speed_ratio_min}`)
      if (this.filters.total_shares_min !== null || this.filters.total_shares_max !== null) {
        parts.push(`总股本${this.filters.total_shares_min || 0}~${this.filters.total_shares_max || '∞' }`)
      }
      if (this.filters.free_shares_min !== null || this.filters.free_shares_max !== null) {
        parts.push(`流通股本${this.filters.free_shares_min || 0}~${this.filters.free_shares_max || '∞' }`)
      }
      if (this.filters.holder_newest_min !== null || this.filters.holder_newest_max !== null) {
        parts.push(`股东数${this.filters.holder_newest_min || 0}~${this.filters.holder_newest_max || '∞' }`)
      }

      // ====== 新增技术面描述 ======
      this.filters.ma_30_break.forEach((field) => {
        if (field === 'breakup_ma_30days') parts.push('突破30日线')
      })
      this.filters.kdj_signals.forEach((field) => {
        if (field === 'kdj_golden_forkz') parts.push('KDJ金叉Z')
        if (field === 'kdj_golden_forky') parts.push('KDJ金叉Y')
        if (field === 'macd_golden_forkz') parts.push('MACD金叉Z')
        if (field === 'macd_golden_forky') parts.push('MACD金叉Y')
      })
      this.filters.pattern_signals.forEach((field) => {
        if (field === 'power_fulgun') parts.push('乌云盖顶')
        if (field === 'pregnant') parts.push('孕线')
        if (field === 'black_cloud_tops') parts.push('黑云压顶')
        if (field === 'narrow_finish') parts.push('窄幅整理')
        if (field === 'reversing_hammer') parts.push('反转锤子')
        if (field === 'first_dawn') parts.push('第一天黎明')
        if (field === 'bearish_engulfing') parts.push('看跌吞没')
        if (field === 'upside_volume') parts.push('上攻放量')
        if (field === 'heaven_rule') parts.push('天道法则')
      })
      this.filters.consecutive_signals.forEach((field) => {
        if (field === 'down_7days') parts.push('连续7天下跌')
        if (field === 'upper_8days') parts.push('连续8天上涨')
        if (field === 'upper_9days') parts.push('连续9天上涨')
        if (field === 'upper_4days') parts.push('连续4天上涨')
      })
      this.filters.volume_trend.forEach((field) => {
        if (field === 'short_avg_array') parts.push('短期均线多头')
        if (field === 'restore_justice') parts.push('复权')
      })

      // ====== 新增资金面描述 ======
      if (this.filters.net_inflow_min !== null) parts.push(`净流入≥${this.filters.net_inflow_min}`)
      if (this.filters.ddx_min !== null) parts.push(`大单动向≥${this.filters.ddx_min}`)
      if (this.filters.netinflow_min_3d !== null) parts.push(`3日净流入≥${this.filters.netinflow_min_3d}`)
      if (this.filters.netinflow_min_5d !== null) parts.push(`5日净流入≥${this.filters.netinflow_min_5d}`)
      if (this.filters.changerate_3d_min !== null) parts.push(`3日涨幅≥${this.filters.changerate_3d_min}%`)
      if (this.filters.changerate_5d_min !== null) parts.push(`5日涨幅≥${this.filters.changerate_5d_min}%`)
      if (this.filters.changerate_10d_min !== null) parts.push(`10日涨幅≥${this.filters.changerate_10d_min}%`)
      if (this.filters.changerate_ty_min !== null || this.filters.changerate_ty_max !== null) {
        parts.push(`年度涨幅${this.filters.changerate_ty_min !== null ? this.filters.changerate_ty_min : '-∞' }~${this.filters.changerate_ty_max !== null ? this.filters.changerate_ty_max : '∞' }%`)
      }

      // ====== 新增股东机构描述 ======
      if (this.filters.holder_change_3m_min !== null) parts.push(`3月持股变动≥${this.filters.holder_change_3m_min}%`)
      if (this.filters.executive_change_3m_min !== null) parts.push(`3月高管持股变动≥${this.filters.executive_change_3m_min}%`)
      if (this.filters.org_rating_filter) parts.push(`机构评级=${this.filters.org_rating_filter}`)
      if (this.filters.allcorp_ratio_min !== null) parts.push(`机构持股比例≥${this.filters.allcorp_ratio_min}%`)
      if (this.filters.allcorp_fund_ratio_min !== null) parts.push(`基金持股≥${this.filters.allcorp_fund_ratio_min}%`)
      if (this.filters.allcorp_qs_ratio_min !== null) parts.push(`券商持股≥${this.filters.allcorp_qs_ratio_min}%`)
      if (this.filters.allcorp_qfii_ratio_min !== null) parts.push(`QFII持股≥${this.filters.allcorp_qfii_ratio_min}%`)

      // ====== 新增状态描述 ======
      this.filters.new_high_filter.forEach((field) => {
        if (field === 'now_newhigh') parts.push('当前新高')
        if (field === 'now_newlow') parts.push('当前新低')
        if (field === 'high_recent_3days') parts.push('3天新高')
        if (field === 'high_recent_5days') parts.push('5天新高')
        if (field === 'high_recent_10days') parts.push('10天新高')
        if (field === 'high_recent_20days') parts.push('20天新高')
        if (field === 'high_recent_30days') parts.push('30天新高')
        if (field === 'low_recent_3days') parts.push('3天新低')
        if (field === 'low_recent_5days') parts.push('5天新低')
        if (field === 'low_recent_10days') parts.push('10天新低')
        if (field === 'low_recent_20days') parts.push('20天新低')
        if (field === 'low_recent_30days') parts.push('30天新低')
      })
      this.filters.win_market_filter.forEach((field) => {
        if (field === 'win_market_3days') parts.push('3天战胜大盘')
        if (field === 'win_market_5days') parts.push('5天战胜大盘')
        if (field === 'win_market_10days') parts.push('10天战胜大盘')
        if (field === 'win_market_20days') parts.push('20天战胜大盘')
        if (field === 'win_market_30days') parts.push('30天战胜大盘')
      })
      this.filters.hs_board_filter.forEach((field) => {
        if (field === 'is_sz50') parts.push('上证50成分股')
        if (field === 'is_zz1000') parts.push('中证1000成分股')
        if (field === 'is_cy50') parts.push('创业板50成分股')
        if (field === 'is_issue_break') parts.push('已破板')
        if (field === 'is_bps_break') parts.push('已破净')
      })

      // ====== 新增派息与质押描述 ======
      if (this.filters.par_dividend_min !== null) parts.push(`派息率≥${this.filters.par_dividend_min}%`)
      if (this.filters.pledge_ratio_max !== null) parts.push(`质押比例≤${this.filters.pledge_ratio_max}%`)
      if (this.filters.goodwill_max !== null) parts.push(`商誉≤${this.filters.goodwill_max}`)

      // ====== 新增限价/定增/质押描述 ======
      this.filters.limited_lift_filter.forEach((field) => {
        if (field === 'limited_lift_6m') parts.push('限价上涨6月')
        if (field === 'limited_lift_1y') parts.push('限价上涨1年')
        if (field === 'limited_lift_f6m') parts.push('限价上涨6月(F)')
        if (field === 'limited_lift_f1y') parts.push('限价上涨1年(F)')
      })
      this.filters.directional_seo_filter.forEach((field) => {
        if (field === 'directional_seo_1m') parts.push('定向增发1月')
        if (field === 'directional_seo_3m') parts.push('定向增发3月')
        if (field === 'directional_seo_6m') parts.push('定向增发6月')
        if (field === 'directional_seo_1y') parts.push('定向增发1年')
        if (field === 'recapitalize_1m') parts.push('定增1月(R)')
        if (field === 'recapitalize_3m') parts.push('定增3月(R)')
        if (field === 'recapitalize_6m') parts.push('定增6月(R)')
        if (field === 'recapitalize_1y') parts.push('定增1年(R)')
      })
      this.filters.equity_pledge_filter.forEach((field) => {
        if (field === 'equity_pledge_1m') parts.push('股权质押1月')
        if (field === 'equity_pledge_3m') parts.push('股权质押3月')
        if (field === 'equity_pledge_6m') parts.push('股权质押6月')
        if (field === 'equity_pledge_1y') parts.push('股权质押1年')
      })
      this.aiQuery = parts.join('; ')
    },

    // 从AI查询框反向解析参数 (此功能已通过handleAiInput部分实现)
    // syncFiltersFromAiQuery () { ... } // 已移除，由handleAiInput替代

    // 分页处理
    handleSizeChange (val) {
      this.pageSize = val
      this.performSearch()
    },
    handleCurrentChange (val) {
      this.currentPage = val
      this.performSearch()
    },
    handleSortChange ({ prop, order }) {
      if (prop) {
        // 实际项目中，这里应重新调用performSearch并传入排序参数
        console.log(`排序: ${prop} ${order}`)
      }
    },

    // 自选相关
    handleSelectionChange (val) {
      this.selectedRows = val
    },
    addSelectedToFavorites () {
      if (this.selectedRows.length === 0) return
      const names = this.selectedRows.map(r => `${r.name}(${r.code})`).join(', ')
      this.$message.success(`已加自选: ${names}`)
      // 实际项目中调用后端API保存
      // fetch('/api/xuangu/favorites/add', { method: 'POST', body: JSON.stringify({ codes: this.selectedRows.map(r => r.code) }) })
    },

    // 滑块值变化处理 - 通用方法
    handleSliderChange (rangeKey, val) {
      const minKey = rangeKey.replace('_range', '_min')
      const maxKey = rangeKey.replace('_range', '_max')
      this.filters[minKey] = val[0]
      this.filters[maxKey] = val[1]
      this.updateAiQuery()
    },

    // 滑块 tooltip 格式化 - 大数字友好显示
    formatSliderTooltip (val, configKey) {
      if (val === null || val === undefined) return ''
      if (val >= 100000000000) return (val / 100000000000).toFixed(1) + '千亿'
      if (val >= 100000000) return (val / 100000000).toFixed(1) + '亿'
      if (val >= 10000) return (val / 10000).toFixed(1) + '万'
      return val
    }
  },
  watch: {
    // 监听所有filters的变化，自动更新AI查询框
    filters: {
      handler () {
        this.updateAiQuery()
      },
      deep: true
    },
    // 监听市场选择变化
    selectedMarket: function () {
      this.updateAiQuery()
    },
    // 筛选面板打开时，从输入框内容反向解析参数填充面板
    filterDialogVisible (val) {
      if (val) {
        this.isUpdatingFromAi = true
        this.resetAllFilters()
        this.parseFilterFromText(this.aiQuery)
        this.$nextTick(() => { this.isUpdatingFromAi = false })
      }
    }
  },
  async mounted () {
    // 初始化AI查询框为空，符合需求
    this.aiQuery = ''

    // 页面加载时获取市场总表数据 (无过滤条件)
    await this.performSearch()
  }
}
</script>

<style scoped>
/* --- 移除全局样式 (body, *) --- */

.xuangu-container {
  font-family: 'Helvetica Neue', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  min-height: 100vh;
  background: linear-gradient(135deg, #f0f2f5 0%, #e8ecf1 100%);
  color: #303133;
  transition: background 0.4s ease;
}

.stock-screener-app {
  max-width: 1900px;
  margin: 0 auto;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
  overflow: hidden;
}

/* --- 市场选择 --- */
.market-filters {
  padding: 16px 24px;
  background: linear-gradient(180deg, #fafbfc 0%, #f5f6f8 100%);
  border-bottom: 1px solid #ebeef5;
}

.market-filters ::v-deep .el-radio-button__inner {
  border-radius: 20px;
  padding: 8px 20px;
  font-size: 13px;
  transition: all 0.25s ease;
  border: 1px solid #dcdfe6;
  margin-right: 8px;
}

.market-filters ::v-deep .el-radio-button__orig-radio:checked + .el-radio-button__inner {
  background: linear-gradient(135deg, #409eff 0%, #337ab7 100%);
  border-color: transparent;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.35);
}

.market-filters ::v-deep .el-radio-button:first-child .el-radio-button__inner,
.market-filters ::v-deep .el-radio-button:last-child .el-radio-button__inner {
  border-radius: 20px;
}

/* --- AI搜索区域 --- */
.ai-search-container {
  display: flex;
  padding: 20px 24px;
  gap: 12px;
  background-color: #fff;
  border-bottom: 1px solid #ebeef5;
  flex-wrap: wrap;
  align-items: flex-start;
}

.filter-trigger-btn {
  flex-shrink: 0;
  height: 56px;
  font-size: 13px;
  border-radius: 8px;
  background: linear-gradient(135deg, #909399 0%, #606266 100%);
  border: none;
  letter-spacing: 1px;
  transition: all 0.3s ease;
}

.filter-trigger-btn:hover {
  background: linear-gradient(135deg, #606266 0%, #404246 100%);
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(96, 98, 102, 0.3);
}

.ai-input {
  flex: 1;
  min-width: 300px;
}

.ai-input ::v-deep .el-textarea__inner {
  border-radius: 8px;
  border: 1px solid #dcdfe6;
  transition: all 0.3s ease;
  font-size: 14px;
  line-height: 1.6;
  padding: 10px 14px;
}

.ai-input ::v-deep .el-textarea__inner:focus {
  border-color: #409eff;
  box-shadow: 0 0 0 3px rgba(64, 158, 255, 0.12);
}

.btn-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.btn-group .el-button {
  width: 110px;
  margin: 0;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
  border-radius: 8px;
  letter-spacing: 1px;
  transition: all 0.3s ease;
}

.btn-group .el-button--primary {
  background: linear-gradient(135deg, #409eff 0%, #337ab7 100%);
  border: none;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.3);
}

.btn-group .el-button--primary:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 14px rgba(64, 158, 255, 0.4);
}

.btn-group .el-button--success {
  background: linear-gradient(135deg, #67c23a 0%, #4da32e 100%);
  border: none;
  box-shadow: 0 2px 8px rgba(103, 194, 58, 0.3);
}

.btn-group .el-button--success:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 14px rgba(103, 194, 58, 0.4);
}

.btn-group .el-button--warning {
  background: linear-gradient(135deg, #e6a23c 0%, #cf8e2e 100%);
  border: none;
  color: #fff;
  box-shadow: 0 2px 8px rgba(230, 162, 60, 0.3);
}

.btn-group .el-button--warning:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 14px rgba(230, 162, 60, 0.4);
}

.btn-group .el-button--info {
  background: linear-gradient(135deg, #909399 0%, #73777d 100%);
  border: none;
  color: #fff;
  box-shadow: 0 2px 8px rgba(144, 147, 153, 0.3);
}

.btn-group .el-button--info:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 14px rgba(144, 147, 153, 0.4);
}

.strategy-conditions-preview {
  padding: 8px 12px;
  background: #f5f7fa;
  border-radius: 4px;
  color: #606266;
  font-size: 13px;
  line-height: 1.6;
  max-height: 120px;
  overflow-y: auto;
  word-break: break-all;
}

/* --- 筛选器面板 --- */
.selector-panel {
  background-color: #fafbfc;
  max-height: 60vh;
  overflow-y: auto;
}

.selector-panel ::v-deep .el-tabs--border-card {
  border: none;
  box-shadow: none;
}

.selector-panel ::v-deep .el-tabs__header {
  background: #f5f6f8;
  border-bottom: 2px solid #ebeef5;
  margin-bottom: 0;
}

.selector-panel ::v-deep .el-tabs__item {
  font-size: 13px;
  height: 40px;
  line-height: 40px;
  transition: all 0.25s ease;
}

.selector-panel ::v-deep .el-tabs__item.is-active {
  color: #409eff;
  font-weight: 600;
  background: #fff;
}

.selector-panel ::v-deep .el-tabs__content {
  padding: 16px 20px;
}

.filter-popover {
  padding: 0 !important;
  max-width: calc(100% - 40px);
  left: 20px !important;
  border-radius: 8px !important;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12) !important;
  border: 1px solid #ebeef5 !important;
}

.filter-section {
  padding: 10px 0;
}

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

/* --- 优化输入框布局 --- */
.range-input {
  display: flex;
  align-items: center;
  gap: 5px;
}

.range-min,
.range-max {
  flex: 1;
  width: calc(20% - 10px);
}

.range-label {
  white-space: nowrap;
  color: #909399;
  font-size: 12px;
}

.single-input {
  width: 100%;
}

/* --- 滑块样式 --- */
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

/* --- 表格和分页 --- */
.result-table {
  padding: 20px 24px;
}

.table-toolbar {
  margin-bottom: 12px;
  display: flex;
  align-items: center;
}

.table-toolbar .el-button--warning {
  background: linear-gradient(135deg, #e6a23c 0%, #cf8e2e 100%);
  border: none;
  border-radius: 6px;
  font-weight: 600;
  transition: all 0.3s ease;
}

.table-toolbar .el-button--warning:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(230, 162, 60, 0.35);
}

.table-scroll-wrapper {
  overflow-x: auto;
  width: 100%;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.04);
}

.table-scroll-wrapper ::v-deep .el-table {
  font-size: 13px;
}

.table-scroll-wrapper ::v-deep .el-table th {
  background: #f5f7fa !important;
  font-weight: 600;
  color: #303133;
  font-size: 12px;
  padding: 10px 0;
}

.table-scroll-wrapper ::v-deep .el-table tr:hover > td {
  background: #ecf5ff !important;
}

.table-scroll-wrapper ::v-deep .el-table td {
  padding: 8px 0;
  transition: background 0.15s ease;
}

.pagination-container {
  margin-top: 16px;
  display: flex;
  justify-content: center;
  padding-bottom: 20px;
}

/* --- 表格文本样式 --- */
::v-deep .text-red {
  color: #f56c6c;
  font-weight: bold;
}

::v-deep .text-green {
  color: #67c23a;
  font-weight: bold;
}

::v-deep .text-grey {
  color: #c0c4cc;
}

/* --- 响应式设计 --- */
@media (max-width: 768px) {
  .ai-search-container {
    flex-direction: column;
    padding: 16px;
  }

  .filter-trigger-btn {
    width: 100%;
    height: 40px;
  }

  .ai-input {
    min-width: 100%;
  }

  .btn-group {
    flex-direction: row;
    width: 100%;
  }

  .btn-group .el-button {
    flex: 1;
    width: auto;
  }

  .range-input {
    flex-direction: column;
    gap: 0;
  }

  .range-min,
  .range-max {
    width: 100%;
    margin-bottom: 5px;
  }

  .slider-wrapper {
    padding: 0;
  }

  .filter-group {
    margin-bottom: 15px;
  }

  .stock-screener-app {
    border-radius: 0;
    overflow-y: auto;
  }
}
</style>
